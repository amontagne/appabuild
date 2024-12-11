"""
Module containing all required classes and methods to run LCA and build impact models.
Majority of the code is copied and adapted from lca_algebraic package.
"""
from __future__ import annotations

import itertools
import types
from collections import OrderedDict
from typing import List, Tuple

import brightway2 as bw
import lca_algebraic as lcaa
from apparun.impact_methods import MethodFullName
from apparun.impact_model import ImpactModel
from apparun.impact_tree import ImpactTreeNode
from apparun.parameters import (
    EnumParam,
    FloatParam,
    ImpactModelParam,
    ImpactModelParams,
)
from bw2data.backends.peewee import Activity
from lca_algebraic import ActivityExtended, with_db_context
from lca_algebraic.base_utils import _getAmountOrFormula, _getDb, debug
from lca_algebraic.helpers import _isForeground
from lca_algebraic.lca import (
    _createTechProxyForBio,
    _multiLCAWithCache,
    _replace_fixed_params,
)
from lca_algebraic.params import _fixed_params, newEnumParam, newFloatParam
from sympy import Expr, simplify, symbols

from appabuild.database.databases import parameters_registry
from appabuild.exceptions import BwDatabaseError, BwMethodError

act_symbols = {}  # Cache of  act = > symbol


def to_bw_method(method_full_name: MethodFullName) -> Tuple[str, str, str]:
    """
    Find corresponding method as known by Brightway.
    :param method_full_name: method to be found.
    :return: Brightway representation of the method.
    """
    matching_methods = [
        method for method in bw.methods if method_full_name in str(method)
    ]
    if len(matching_methods) < 1:
        raise BwMethodError(f"Cannot find method {method_full_name}.")
    if len(matching_methods) > 1:
        raise BwMethodError(
            f"Too many methods matching {method_full_name}: {matching_methods}."
        )
    return matching_methods[0]


class ImpactModelBuilder:
    """
    Main purpose of this class is to build Impact Models.
    """

    def __init__(self, database_name: str):
        """
        Initialisation.
        :param database_name: user database name.
        """
        self.database_name = database_name
        self.database = bw.Database(database_name)

    def build_impact_model(
        self, functional_unit: str, methods: List[str], metadata: dict
    ) -> ImpactModel:
        """
        Build an Impact Model
        :param functional_unit: uuid of the activity producing the reference flow.
        :param methods: list of methods to generate arithmetic models for. Expected
        method format is Appa Run method keys.
        :param metadata: information about the LCA behind the impact model. Should
        contain, or link to all information necessary for the end user's proper
        understanding of the impact model.

        :return: built impact model.
        """
        functional_unit_bw = [i for i in self.database if functional_unit == i["name"]]
        if len(functional_unit_bw) < 1:
            raise BwDatabaseError(f"Cannot find activity {functional_unit} for FU.")
        if len(functional_unit_bw) > 1:
            raise BwDatabaseError(
                f"Too many activities matching {functional_unit} for FU: "
                f"{functional_unit_bw}."
            )
        functional_unit_bw = functional_unit_bw[0]
        tree, params = self.build_impact_tree_and_parameters(
            functional_unit_bw, methods
        )
        impact_model = ImpactModel(tree=tree, parameters=params, metadata=metadata)
        return impact_model

    def build_impact_tree_and_parameters(
        self, functional_unit_bw: ActivityExtended, methods: List[str]
    ) -> Tuple[ImpactTreeNode, ImpactModelParams]:
        """
        Perform LCA, construct all arithmetic models and collect used parameters.
        :param functional_unit_bw: Brightway activity producing the reference flow.
        :param methods: list of methods to generate arithmetic models for. Expected
        method format is Appa Run method keys.
        :return: root node (corresponding to the reference flow) and used parameters.
        """
        methods_bw = [to_bw_method(MethodFullName[method]) for method in methods]
        tree = ImpactTreeNode(name=functional_unit_bw["name"], amount=1)
        # print("computing model to expression for %s" % model)
        self.actToExpression(functional_unit_bw, tree)

        # Find required parameters by inspecting symbols
        free_symbols = set(
            list(
                itertools.chain.from_iterable(
                    [
                        [str(symb) for symb in node._raw_direct_impact.free_symbols]
                        for node in tree.unnested_descendants
                    ]
                )
            )
            + list(
                itertools.chain.from_iterable(
                    [
                        [str(symb) for symb in node.amount.free_symbols]
                        for node in tree.unnested_descendants
                        if isinstance(node.amount, Expr)
                    ]
                )
            )
        )
        activity_symbols = set([str(symb["symbol"]) for _, symb in act_symbols.items()])

        expected_parameter_symbols = free_symbols - activity_symbols

        known_parameters = ImpactModelParams.from_list(parameters_registry.values())

        forbidden_parameter_names = list(
            itertools.chain(
                *[
                    [
                        elem.name
                        for elem in known_parameters.find_corresponding_parameter(
                            activity_symbol, must_find_one=False
                        )
                    ]
                    for activity_symbol in activity_symbols
                ]
            )
        )

        if len(forbidden_parameter_names) > 0:
            raise ValueError(
                f"Parameter names {forbidden_parameter_names} are forbidden as they "
                f"correspond to background activities."
            )

        used_parameters = [
            known_parameters.find_corresponding_parameter(expected_parameter_symbol)
            for expected_parameter_symbol in expected_parameter_symbols
        ]
        unique_used_parameters = []
        [
            unique_used_parameters.append(i)
            for i in used_parameters
            if i not in unique_used_parameters
        ]
        unique_used_parameters = ImpactModelParams.from_list(unique_used_parameters)
        # Declare used parameters in conf file as a lca_algebraic parameter to enable
        # model building (will not be used afterwards)
        for parameter in unique_used_parameters:
            if isinstance(parameter, FloatParam):
                newFloatParam(
                    name=parameter.name, default=parameter.default, save=False
                )
            if isinstance(parameter, EnumParam):
                newEnumParam(
                    name=parameter.name,
                    values=parameter.weights,
                    default=parameter.default,
                )
        # Create dummy reference to biosphere
        # We cannot run LCA to biosphere activities
        # We create a technosphere activity mapping exactly to 1 biosphere item
        pureTechActBySymbol = OrderedDict()
        for act, name in [
            (act, name) for act, name in act_symbols.items() if name["to_compile"]
        ]:
            pureTechActBySymbol[name["symbol"]] = _createTechProxyForBio(
                act, functional_unit_bw.key[0]
            )

        # Compute LCA for background activities
        lcas = _multiLCAWithCache(pureTechActBySymbol.values(), methods_bw)

        # For each method, compute an algebric expression with activities replaced by their values
        for node in tree.unnested_descendants:
            model_expr = node._raw_direct_impact
            for method in methods:
                # Replace activities by their value in expression for this method
                sub = dict(
                    {
                        symbol: lcas[(act, to_bw_method(MethodFullName[method]))]
                        for symbol, act in pureTechActBySymbol.items()
                    }
                )
                node.direct_impacts[method] = model_expr.xreplace(sub)
        return tree, unique_used_parameters

    @staticmethod
    @with_db_context
    def actToExpression(act: Activity, impact_model_tree_node: ImpactTreeNode):
        """
        Determines the arithmetic model corresponding to activity's impact function of
        model's parameters.
        :param act: Brightway activity corresponding to the node.
        :param impact_model_tree_node: node of the tree to store result in.
        :return:
        """

        def act_to_symbol(sub_act, to_compile: bool = True):
            """Transform an activity to a named symbol and keep cache of it"""

            db_name, code = sub_act.key

            # Look in cache
            if not (db_name, code) in act_symbols:
                act = _getDb(db_name).get(code)
                name = act["name"]
                base_slug = ImpactTreeNode.node_name_to_symbol_name(name)

                slug = base_slug
                i = 1
                while symbols(slug) in [
                    act_symbol["symbol"] for act_symbol in list(act_symbols.values())
                ]:
                    slug = f"{base_slug}{i}"
                    i += 1

                act_symbols[(db_name, code)] = {
                    "symbol": symbols(slug),
                    "to_compile": to_compile,
                }

            return act_symbols[(db_name, code)]["symbol"]

        def rec_func(act: Activity, impact_model_tree_node: ImpactTreeNode):
            res = 0
            outputAmount = act.getOutputAmount()

            if not _isForeground(act["database"]):
                # We reached a background DB ? => stop developping and create reference
                # to activity
                return act_to_symbol(act)

            for exch in act.exchanges():
                amount = _getAmountOrFormula(exch)
                if isinstance(amount, types.FunctionType):
                    # Some amounts in EIDB are functions ... we ignore them
                    continue

                #  Production exchange
                if exch["input"] == exch["output"]:
                    continue

                input_db, input_code = exch["input"]
                sub_act = _getDb(input_db).get(input_code)

                # Background DB or tracked foreground activity => reference it as a
                # symbol
                if not _isForeground(input_db):
                    act_expr = act_to_symbol(sub_act)
                else:
                    if impact_model_tree_node.name_already_in_tree(sub_act["name"]):
                        raise Exception(f"Found recursive activity: {sub_act['name']}")
                    if sub_act["include_in_tree"]:
                        # act_expr = act_to_symbol(sub_act, to_compile=False)
                        ImpactModelBuilder.actToExpression(
                            sub_act,
                            impact_model_tree_node.new_child(
                                name=sub_act["name"], amount=amount
                            ),
                        )
                        amount = 1  # amount is already handled in tree node
                        act_expr = 0  # no direct impact
                    # Our model : recursively it to a symbolic expression
                    else:
                        act_expr = rec_func(sub_act, impact_model_tree_node)

                avoidedBurden = 1

                if exch.get("type") == "production" and not exch.get(
                    "input"
                ) == exch.get("output"):
                    debug("Avoided burden", exch[lcaa.helpers.name])
                    avoidedBurden = -1

                # debug("adding sub act : ", sub_act, formula, act_expr)

                res += amount * act_expr * avoidedBurden

            return res / outputAmount

        expr = rec_func(act, impact_model_tree_node)

        if isinstance(expr, float):
            expr = simplify(expr)
        else:
            # Replace fixed params with their default value
            expr = _replace_fixed_params(expr, _fixed_params().values())
        impact_model_tree_node._raw_direct_impact = expr
