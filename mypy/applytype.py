from __future__ import annotations

from typing import Callable, Sequence

import mypy.subtypes
from mypy.expandtype import expand_type, expand_unpack_with_variables
from mypy.nodes import ARG_STAR, Context
from mypy.types import (
    AnyType,
    CallableType,
    Parameters,
    ParamSpecType,
    PartialType,
    TupleType,
    Type,
    TypeVarId,
    TypeVarLikeType,
    TypeVarTupleType,
    TypeVarType,
    UnpackType,
    get_proper_type,
)
from mypy.typevartuples import find_unpack_in_list, replace_starargs


def get_target_type(
    tvar: TypeVarLikeType,
    type: Type,
    callable: CallableType,
    report_incompatible_typevar_value: Callable[[CallableType, Type, str, Context], None],
    context: Context,
    skip_unsatisfied: bool,
) -> Type | None:
    if isinstance(tvar, ParamSpecType):
        return type
    if isinstance(tvar, TypeVarTupleType):
        return type
    assert isinstance(tvar, TypeVarType)
    values = tvar.values
    p_type = get_proper_type(type)
    if values:
        if isinstance(p_type, AnyType):
            return type
        if isinstance(p_type, TypeVarType) and p_type.values:
            # Allow substituting T1 for T if every allowed value of T1
            # is also a legal value of T.
            if all(any(mypy.subtypes.is_same_type(v, v1) for v in values) for v1 in p_type.values):
                return type
        matching = []
        for value in values:
            if mypy.subtypes.is_subtype(type, value):
                matching.append(value)
        if matching:
            best = matching[0]
            # If there are more than one matching value, we select the narrowest
            for match in matching[1:]:
                if mypy.subtypes.is_subtype(match, best):
                    best = match
            return best
        if skip_unsatisfied:
            return None
        report_incompatible_typevar_value(callable, type, tvar.name, context)
    else:
        upper_bound = tvar.upper_bound
        if not mypy.subtypes.is_subtype(type, upper_bound):
            if skip_unsatisfied:
                return None
            report_incompatible_typevar_value(callable, type, tvar.name, context)
    return type


def apply_generic_arguments(
    callable: CallableType,
    orig_types: Sequence[Type | None],
    report_incompatible_typevar_value: Callable[[CallableType, Type, str, Context], None],
    context: Context,
    skip_unsatisfied: bool = False,
    allow_erased_callables: bool = False,
) -> CallableType:
    """Apply generic type arguments to a callable type.

    For example, applying [int] to 'def [T] (T) -> T' results in
    'def (int) -> int'.

    Note that each type can be None; in this case, it will not be applied.

    If `skip_unsatisfied` is True, then just skip the types that don't satisfy type variable
    bound or constraints, instead of giving an error.
    """
    tvars = callable.variables
    assert len(tvars) == len(orig_types)
    # Check that inferred type variable values are compatible with allowed
    # values and bounds.  Also, promote subtype values to allowed values.
    # Create a map from type variable id to target type.
    id_to_type: dict[TypeVarId, Type] = {}

    for tvar, type in zip(tvars, orig_types):
        assert not isinstance(type, PartialType), "Internal error: must never apply partial type"
        if type is None:
            continue

        target_type = get_target_type(
            tvar, type, callable, report_incompatible_typevar_value, context, skip_unsatisfied
        )
        if target_type is not None:
            id_to_type[tvar.id] = target_type

    param_spec = callable.param_spec()
    if param_spec is not None:
        nt = id_to_type.get(param_spec.id)
        if nt is not None:
            nt = get_proper_type(nt)
            if isinstance(nt, (CallableType, Parameters)):
                callable = callable.expand_param_spec(nt)

    # Apply arguments to argument types.
    var_arg = callable.var_arg()
    if var_arg is not None and isinstance(var_arg.typ, UnpackType):
        star_index = callable.arg_kinds.index(ARG_STAR)
        callable = callable.copy_modified(
            arg_types=(
                [
                    expand_type(at, id_to_type, allow_erased_callables)
                    for at in callable.arg_types[:star_index]
                ]
                + [callable.arg_types[star_index]]
                + [
                    expand_type(at, id_to_type, allow_erased_callables)
                    for at in callable.arg_types[star_index + 1 :]
                ]
            )
        )

        unpacked_type = get_proper_type(var_arg.typ.type)
        if isinstance(unpacked_type, TupleType):
            # Assuming for now that because we convert prefixes to positional arguments,
            # the first argument is always an unpack.
            expanded_tuple = expand_type(unpacked_type, id_to_type)
            if isinstance(expanded_tuple, TupleType):
                # TODO: handle the case where the tuple has an unpack. This will
                # hit an assert below.
                expanded_unpack = find_unpack_in_list(expanded_tuple.items)
                if expanded_unpack is not None:
                    callable = callable.copy_modified(
                        arg_types=(
                            callable.arg_types[:star_index]
                            + [expanded_tuple]
                            + callable.arg_types[star_index + 1 :]
                        )
                    )
                else:
                    callable = replace_starargs(callable, expanded_tuple.items)
            else:
                # TODO: handle the case for if we get a variable length tuple.
                assert False, f"mypy bug: unimplemented case, {expanded_tuple}"
        elif isinstance(unpacked_type, TypeVarTupleType):
            expanded_tvt = expand_unpack_with_variables(var_arg.typ, id_to_type)
            assert isinstance(expanded_tvt, list)
            for t in expanded_tvt:
                assert not isinstance(t, UnpackType)
            callable = replace_starargs(callable, expanded_tvt)
        else:
            assert False, "mypy bug: unhandled case applying unpack"
    else:
        callable = callable.copy_modified(
            arg_types=[
                expand_type(at, id_to_type, allow_erased_callables) for at in callable.arg_types
            ]
        )

    # Apply arguments to TypeGuard if any.
    if callable.type_guard is not None:
        type_guard = expand_type(callable.type_guard, id_to_type, allow_erased_callables)
    else:
        type_guard = None

    # The callable may retain some type vars if only some were applied.
    remaining_tvars = [tv for tv in tvars if tv.id not in id_to_type]

    return callable.copy_modified(
        ret_type=expand_type(callable.ret_type, id_to_type, allow_erased_callables),
        variables=remaining_tvars,
        type_guard=type_guard,
    )
