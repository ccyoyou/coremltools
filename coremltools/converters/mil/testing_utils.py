#  Copyright (c) 2020, Apple Inc. All rights reserved.
#
#  Use of this source code is governed by a BSD-3-clause license that can be
#  found in the LICENSE.txt file or at https://opensource.org/licenses/BSD-3-Clause
import copy
import itertools
import os
from functools import partial
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

import numpy as np
import pytest
from PIL import Image

import coremltools as ct
import coremltools.models.utils as coremltoolsutils
from coremltools import proto
from coremltools._deps import _IS_MACOS
from coremltools.converters.mil import mil
from coremltools.converters.mil.mil import Block, Function, Program
from coremltools.converters.mil.mil.passes.defs.preprocess import NameSanitizer as _NameSanitizer
from coremltools.converters.mil.mil.passes.graph_pass import AbstractGraphPass
from coremltools.converters.mil.mil.passes.pass_registry import PASS_REGISTRY
from coremltools.converters.mil.mil.scope import ScopeSource

np.random.seed(10)

# The minimum macOS version for an IOS target. For example, iOS16 target requires macOS13+.
IOS_TO_MINIMUM_MACOS_VERSION: Dict[ct.target, int] = {
    ct.target.iOS14: 11,
    ct.target.iOS15: 12,
    ct.target.iOS16: 13,
    ct.target.iOS17: 14,
    ct.target.iOS18: 15,
}

_COREMLTOOLS_DEBUG_SAVE_MLMODEL_DIRECTORY = "/tmp/coremltools_debug_save_mlmodel"

debug_save_mlmodels = set()
debug_save_mlmodel_config_file_name = os.environ.get("DEBUG_SAVE_MLMODEL", "0")
if debug_save_mlmodel_config_file_name != "0":
    if not os.path.isfile(debug_save_mlmodel_config_file_name):
        raise ValueError("DEBUG_SAVE_MLMODEL must be the name of a config file with tests to save")
    with open(debug_save_mlmodel_config_file_name, "r") as f:
        lines = f.readlines()
        for line in lines:
            if line[0] == "#" or line == "\n":
                continue
            debug_save_mlmodels.add(line[:-1])

hardcoded_einsum_equations: List[str] = [
    # hardcoded cases
    "abcd,adce->abce",
    "abc,cbd->abd",
    "bnqd,bnkd->bnqk",
    "abc,cd->abd",
    "abc,cde->abde",
    "btnh,bfnh->bnft",
    "bnft,btnh->bfnh",
    "abcd,cde->abe",
    "a b c d , a d c e -> a b c e",
]

einsum_equations: List[str] = hardcoded_einsum_equations + [
    # with-diagonal generic cases
    "jiii,ijjk->jk",
    "iji,ji->j",
    "jii,ijk->jk",
    "ijij,iij->ij",
    # no-diagonal generic cases
    "i,j->ij",  # outer product
    "a,a->a",  # batched outer product
    "ija,la->ijal",  # batched outer product
    "i,i->",  # inner product
    "ia,ia->a",  # batched inner product
    "ai,ia->a",  # batched inner product
    "abi,abi->ab",  # batched inner product
    "iab,iab->ab",  # batched inner product
    "abi,bai->ba",  # batched inner product
    "ij,j->i",  # matrix-vector multiplication
    "i,ij->j",  # vector-matrix multiplication
    "ai,ija->aj",  # batched vector-matrix multiplication
    "aibj,bi->jba",  # batched matrix-vector multiplication
    "ij,jk->ik",  # matrix multiplication
    "aij,ajk->iak",  # batched matrix multiplication
    "abij,abjk->abik",  # batched matrix multiplication
    "aijb,bajk->abik",  # batched matrix multiplication
    "ij,ij->",  # double-inner product
    "ij,ji->",  # double-inner product
    "aij,aij->a",  # batched double-inner product
    "ija,ija->a",  # batched double-inner product
    "ija,jia->a",  # batched double-inner product
    "aijb,ajbi->ab",  # batched double-inner product
    "aibj,cdij->cadb",  # batched double-inner product
    "ijk,lmj->iklm",  # 3rd-order tensor contraction
    "ijak,akl->aijl",  # batched 3rd-order tensor and matrix contraction
    # Generic with sum
    "ij,j->ij",
    "ij,kjl->j",
    "iijj,j->j",
]


def macos_compatible_with_deployment_target(minimum_deployment_target):
    if coremltoolsutils._is_macos():
        macos_major_version = coremltoolsutils._macos_version()[0]
        if macos_major_version < IOS_TO_MINIMUM_MACOS_VERSION[minimum_deployment_target]:
            return False
    return True


def _create_current_pytest_serialization_path() -> str:
    serialization_path = _COREMLTOOLS_DEBUG_SAVE_MLMODEL_DIRECTORY + "/"

    PYTEST_CURRENT_TEST = os.environ.get("PYTEST_CURRENT_TEST").split("(call)")[0].strip()
    test_name_fragments = PYTEST_CURRENT_TEST.split("::")

    for test_name_fragment in test_name_fragments[:-1]:
        serialization_path += f"{test_name_fragment.strip()}/"

    test_name = test_name_fragments[-1]
    # For a parameterized test, further decompose parameters into directories
    if "[" in test_name and test_name[-1] == "]":
        # Split test name with []
        bra_index = test_name.index("[")
        test_function_name = test_name[:bra_index]
        parameters = test_name[bra_index + 1 : -1].split("-")
        # Append test function name and parameter to mlpackage path
        serialization_path += f"{test_function_name}/"
        for parameter in parameters:
            serialization_path += f"{parameter}/"
    else:
        serialization_path += f"{test_name}/"

    return serialization_path


def _serialize_current_pytest_mlmodel(mlmodel) -> None:
    """
    Usually pytest test name is of format file::class::test_function[param0-param1] (call)...
    Assume each test produces only one Core ML model,
    then file::class::test_function[param0-param1] is enough to determine unique name
        {_COREMLTOOLS_DEBUG_SAVE_MLMODEL_DIRECTORY}/file/class/test_function/param0/param1/model.mlpackage
    """
    mlpackage_path = _create_current_pytest_serialization_path() + "model.mlpackage"
    Path(mlpackage_path).mkdir(parents=True, exist_ok=True)
    mlmodel.save(mlpackage_path)


def str_to_proto_feature_type(dtype: str) -> "proto.FeatureTypes_pb2.ArrayFeatureType":
    if dtype == "int32":
        return proto.FeatureTypes_pb2.ArrayFeatureType.INT32
    elif dtype == "fp32":
        return proto.FeatureTypes_pb2.ArrayFeatureType.FLOAT32
    elif dtype == "fp16":
        return proto.FeatureTypes_pb2.ArrayFeatureType.FLOAT16
    else:
        raise TypeError(f"{dtype} doesn't have a corresponding protobuf feature type")


def assert_op_count_match(program, expect, op=None, verbose=False):
    """
    Assert number of ops match expected number. If op is not specified,
    Count total number of ops and match with expect.
    """
    if verbose:
        print(program)

    count = 0
    for _, func in program.functions.items():
        for o in func.operations:
            if not op:
                count += 1
            elif o.op_type.lower() == op.lower():
                count += 1
        np.testing.assert_equal(count, expect)


def assert_model_is_valid(
    program,
    inputs,
    backend=("neuralnetwork", "fp32"),
    verbose=True,
    expected_output_shapes=None,
    minimum_deployment_target: ct.target = None,
):
    """
    Assert Core ML model is valid.

    Inputs:

    - input: str -> shape tuple. All program input names need to appear in str.
      shape tuple can only contain positive integers.
    """
    if minimum_deployment_target is not None:
        validate_minimum_deployment_target(minimum_deployment_target, backend)

    # Avoid circular import
    from coremltools.converters.mil.testing_reqs import ct

    input_dict = dict()
    for name, shape in inputs.items():
        input_dict[name] = np.random.rand(*shape)

    mlmodel = ct_convert(
        program,
        source="milinternal",
        convert_to=backend,
        compute_units=ct.ComputeUnit.CPU_ONLY,
        minimum_deployment_target=minimum_deployment_target,
    )
    assert mlmodel is not None

    if verbose:
        from coremltools.models.neural_network.printer import print_network_spec
        print_network_spec(mlmodel.get_spec(), style="coding")

    if _IS_MACOS and (not mlmodel.is_package or coremltoolsutils._macos_version() >= (12, 0)):
        prediction = mlmodel.predict(input_dict)
        assert prediction is not None
        if expected_output_shapes is not None:
            for out_name, out_shape in expected_output_shapes.items():
                assert out_name in prediction
                assert out_shape == prediction[out_name].shape, \
                        "{} != {}".format(out_shape, prediction[out_name].shape)

def assert_same_input_names(prog1, prog2, func_name="main"):
    # check the input keys
    prog1_input_keys = list(prog1[func_name].inputs.keys())
    prog2_input_keys = list(prog2[func_name].inputs.keys())
    assert prog1_input_keys == prog2_input_keys

    # check the input var name
    prog1_input_names = [x.name for x in list(prog1[func_name].inputs.values())]
    prog2_input_names = [x.name for x in list(prog2[func_name].inputs.values())]
    assert prog1_input_names == prog2_input_names


def assert_numerical_value(mil_var, expected_value):
    if mil_var is None:
        assert expected_value is None
    else:
        np.testing.assert_allclose(mil_var.val, expected_value)


def assert_same_input_types(prog1, prog2, func_name="main"):
    prog1_input_types = [x.dtype for x in list(prog1[func_name].inputs.values())]
    prog2_input_types = [x.dtype for x in list(prog2[func_name].inputs.values())]
    assert prog1_input_types == prog2_input_types

def assert_same_output_names(prog1, prog2, func_name="main"):
    prog1_outputs = [o.name for o in prog1[func_name].outputs]
    prog2_outputs = [o.name for o in prog2[func_name].outputs]
    assert prog1_outputs == prog2_outputs

def assert_same_output_types(prog1: Program, prog2: Program, func_name: str = "main"):
    """
    Check ``prog1`` and ``prog2`` have the same output dtypes.
    """
    prog1_output_types = [o.dtype for o in prog1[func_name].outputs]
    prog2_output_types = [o.dtype for o in prog2[func_name].outputs]
    assert prog1_output_types == prog2_output_types

def assert_same_output_shapes(prog1, prog2, func_name="main"):
    prog1_output_shapes = [o.shape for o in prog1[func_name].outputs]
    prog2_output_shapes = [o.shape for o in prog2[func_name].outputs]
    assert prog1_output_shapes == prog2_output_shapes


def gen_activation_stats_for_program(prog):
    """
    Return a dictionary of activation_stats for all intermediate tensors.
    """
    tensor_list = get_op_names_in_program(prog)
    activation_stats = {}
    for tensor_name in tensor_list:
        activation_stats[tensor_name] = {"rmin": 0, "rmax": 1}
    return activation_stats

def get_op_names_in_program(prog, func_name="main", skip_const_ops=True):
    """
    Return the operations names in prog[func_name],
    in the same order as they are stored (topological)
    """
    op_names_in_program = []
    for op in prog[func_name].operations:
        if skip_const_ops:
            if op.op_type == "const":
                continue
        op_names_in_program.append(op.name)
    return op_names_in_program


def get_op_types_in_block(block: Block, skip_const_ops: bool = True, recurse: bool = False):
    """
    Return the operation types in block,
    in the same order as they are stored (topological)
    """
    op_types_in_block = []
    for op in block.operations:
        if skip_const_ops:
            if op.op_type == "const":
                continue
        op_types_in_block.append(op.op_type)

        if recurse:
            for child_block in op.blocks:
                child_ops = get_op_types_in_block(child_block, skip_const_ops, recurse)
                op_types_in_block += child_ops

    return op_types_in_block


def get_op_types_in_program(prog: Program, func_name: str = "main", skip_const_ops: bool = True, recurse: bool = False):
    """
    Return the operation types in prog[func_name],
    in the same order as they are stored (topological)
    If ``skip_const_ops = True``, const ops are not returned.
    If ``recurse = True``, the ops of all nested blocks are returned.
    """
    return get_op_types_in_block(prog[func_name], skip_const_ops, recurse)

def random_gen(
    shape,
    rand_min=0.0,
    rand_max=1.0,
    eps_from_int=0.0,
    allow_duplicate=True,
    dtype=np.float32,
):
    """
    This helper function generates a random array of shape `shape`
    The range of generated numbers will be between (rand_min, rand_max].
    The value of generated numbers will be at least `eps_from_int` apart from integers.
    If allow_duplicate is set to false, it is guaranteed that value generated are all different.
    Default data type is np.float32.
    """
    elem = np.prod(shape).astype(np.int32)

    # Since this function is extensively used as well for the fp16 precision models,
    # we make sure that the numerical value can be presented in fp16.
    gen_dtype = np.float16 if dtype == np.float32 else dtype
    ret = []
    for _ in range(elem):
        while True:
            r = gen_dtype((rand_max - rand_min) * np.random.random() + rand_min)
            if not allow_duplicate and r in ret:
                continue
            if np.issubdtype(gen_dtype, np.integer) or np.fabs(np.round(r) - r) > eps_from_int:
                ret.append(r)
                break
    ret = np.array(ret).reshape(shape)
    return ret.astype(dtype)


def ssa_fn(func):
    """
    Deprecated: use @mb.program()
    """

    def wrapper(*args, **kwargs):
        prog = mil.Program()
        with Function({}) as ssa_func:
            func(*args, **kwargs)

    return wrapper


def to_tuple(v):
    if not isinstance(v, (list, tuple)):
        return tuple([v])
    return tuple(v)


def run_core_ml_predict(mlmodel, input_key_values, state=None):
    for k, v in input_key_values.items():
        if isinstance(v, Image.Image):
            continue
        elif not np.isscalar(v) and not v.shape == ():
            input_key_values[k] = v.astype(np.float32)
        else:
            input_key_values[k] = np.array([v], dtype=np.float32)
    return mlmodel.predict(input_key_values, state=state)

def _get_coreml_out_from_dict(out_dict, out_name):
    if out_name in out_dict:
        return out_dict[out_name]
    sanitized_out_name = _NameSanitizer._replace_invalid_char_with_underscore(out_name)
    if sanitized_out_name in out_dict:
        return out_dict[sanitized_out_name]
    else:
        raise KeyError(f"{out_name} output not found in Core ML outputs")


def _get_proto_output_shape(desc, out_name):
    sanitized_out_name = _NameSanitizer._replace_invalid_char_with_underscore(out_name)
    for coreml_o in desc.output:
        if coreml_o.name == sanitized_out_name:
            return coreml_o.type.multiArrayType.shape
    raise KeyError(f"{out_name} output not found in Core ML outputs")

def compare_backend(
    mlmodel,
    input_key_values,
    expected_outputs,
    dtype="fp32",
    atol=1e-04,
    rtol=1e-05,
    also_compare_shapes=True,
    state=None,
    allow_mismatch_ratio=0.0,
):
    """
    Inputs:
        - mlmodel: MLModel.

        - input_key_values: str -> np.array. Keys must match those in
          input_placeholders.

        - expected_outputs: dict[str, np.array]. Required iff
          frontend_only is False

        - allow_mismatch_ratio: Allow a ratio of elements to be out of tolenrance of atol and rtol. Mainly used
          for comparing compressed models outputs.
    """
    if _IS_MACOS and (not mlmodel.is_package or coremltoolsutils._macos_version() >= (12, 0)):

        if dtype not in ["fp32", "fp16"]:
            raise ValueError("Unsupported dtype config")

        pred = run_core_ml_predict(mlmodel, input_key_values, state)
        if also_compare_shapes:
            compare_shapes(
                mlmodel,
                input_key_values,
                expected_outputs,
                pred=pred,
            )
        if mlmodel.compute_unit != ct.ComputeUnit.CPU_ONLY or (dtype == "fp16"):
            atol = max(atol * 100.0, 5e-1)
            rtol = max(rtol * 100.0, 5e-2)
        for o, expected in expected_outputs.items():
            coreml_out = _get_coreml_out_from_dict(pred, o)

            if isinstance(coreml_out, np.ndarray):
                try:
                    np.testing.assert_allclose(coreml_out, expected, atol=atol, rtol=rtol)
                except AssertionError as e:
                    mismatch_num = np.sum(~np.isclose(coreml_out, expected, atol=atol, rtol=rtol))
                    total_num = np.prod(expected.shape)
                    if mismatch_num / total_num > allow_mismatch_ratio:
                        raise e
            elif isinstance(coreml_out, dict):
                for k, v in coreml_out.items():
                    assert k in expected
                    assert expected[k] == v
            else:
                assert coreml_out == expected

        return pred
    return None


def compare_shapes(mlmodel, input_key_values, expected_outputs, pred=None):
    """
    Inputs:
        - mlmodel: MLModel.

        - input_key_values: str -> np.array or PIL.Image. Keys must match those in
          input_placeholders.

        - expected_outputs: dict[str, np.array].

        - pred: Prediction to use, if it has already been computed.
    """
    if _IS_MACOS:
        if not pred:
            pred = run_core_ml_predict(mlmodel, input_key_values)
        for o, expected in expected_outputs.items():
            coreml_out = _get_coreml_out_from_dict(pred, o)

            # output is dictionary (for classifier)
            if isinstance(coreml_out, dict) and isinstance(expected, dict):
                assert len(coreml_out) == len(expected)
                continue

            # output is numpy objects
            np_types = (np.generic, np.ndarray)
            if isinstance(coreml_out, np_types) and isinstance(expected, np_types):
                msg = "Output: {}. expected shape {} != actual shape {}".format(
                    o, expected.shape, coreml_out.shape
                )
                # Core ML does not support scalar as output
                # remove this special case when support is added
                if expected.shape == () and coreml_out.shape == (1,):
                    continue
                assert coreml_out.shape == expected.shape, msg

                # Validate the shape consistency across runtime returned values and
                # the output information in the mlprogram proto.
                spec = mlmodel.get_spec()
                if spec.WhichOneof("Type") == "mlProgram":

                    if mlmodel._is_multifunction():
                        desc = mlmodel._get_function_description(mlmodel.function_name)
                    else:
                        desc = spec.description

                    # The proto output and the runtime outputs are different for classifier
                    if desc.predictedFeatureName != "":
                        continue

                    proto_shape = _get_proto_output_shape(desc, o)

                    if proto_shape != []:
                        assert proto_shape == list(
                            coreml_out.shape
                        ), f"the output shape, for output named {o}, returned by the model is {coreml_out.shape} which does match with the shape present in the proto spec, which is {proto_shape}"
                continue

            # output is other types (for classifier)
            assert type(coreml_out) == type(expected)

def ct_convert(
    program,
    source="auto",
    inputs=None,
    outputs=None,
    classifier_config=None,
    minimum_deployment_target=None,
    convert_to=None,
    compute_precision=None,
    skip_model_load=False,
    converter=ct.convert,
    **kwargs,
):

    """
    Overloaded ct.convert function with the only difference being in the argument `convert_to`
    which in this overloaded call accepts a tuple of (target, dtype).
    Ex: ("neuralnetwork", "fp32"), ("mlprogram", "fp16")
    """

    if isinstance(converter, partial):
        raise ValueError("Partial function is not supported for function-parameter 'converter' since its keywords arguments could get overridden.")

    target, dtype = convert_to

    if dtype not in ["fp32", "fp16"]:
        raise ValueError("Unsupported dtype config")

    compute_precision = ct.precision.FLOAT16 if dtype == "fp16" else ct.precision.FLOAT32
    if target == "neuralnetwork":
        compute_precision = None

    PYTEST_CURRENT_TEST = os.environ.get("PYTEST_CURRENT_TEST").split("(call)")[0].strip()
    is_current_test_to_be_debugged = PYTEST_CURRENT_TEST in debug_save_mlmodels
    if is_current_test_to_be_debugged:
        # If current test is to be debugged, then it is probably buggy in Core ML framework,
        # so we skip its load to dodge potential bug which might kill python process
        skip_model_load = True

    mlmodel = converter(
        program,
        source=source,
        inputs=inputs,
        outputs=outputs,
        classifier_config=classifier_config,
        minimum_deployment_target=minimum_deployment_target,
        convert_to=target,
        compute_precision=compute_precision,
        skip_model_load=skip_model_load,
        **kwargs,
    )

    if is_current_test_to_be_debugged:
        _serialize_current_pytest_mlmodel(mlmodel)
        pytest.xfail("This test is to be debugged")

    return mlmodel

def get_core_ml_prediction(
    build, input_placeholders, input_values, backend, compute_unit=ct.ComputeUnit.CPU_ONLY
):
    """
    Return predictions of the given model.
    """
    minimum_deployment_target = backend.opset_version
    program = mil.Program()
    with Function(input_placeholders, opset_version=minimum_deployment_target) as ssa_func:
        output_vars = build(**ssa_func.inputs)
        if isinstance(output_vars, tuple):
            output_vars = list(output_vars)
        elif not isinstance(output_vars, list):
            output_vars = [output_vars]
        ssa_func.set_outputs(output_vars)
        program.add_function("main", ssa_func)

    mlmodel = ct_convert(
        program,
        source="milinternal",
        convert_to=(backend.backend, backend.precision),
        compute_units=compute_unit,
        minimum_deployment_target=minimum_deployment_target,
    )
    return mlmodel.predict(input_values)


def _decorate_prog_with_scope_if_not_present(prog: Program):
    """
    For a program without any scope info, we manually add scopes to every op,
    in ordere to test that all graph passes can preserve the source scope info.
    """

    def _is_scopes_present_in_program(prog: Program) -> bool:
        """
        Return True is any op already has the scopes info.
        """

        def _is_scopes_present_in_block(block: Block) -> bool:
            for op in block.operations:
                for b in op.blocks:
                    if _is_scopes_present_in_block(b):
                        return True
                if len(op.scopes) > 0:
                    return True

        for func in prog.functions.values():
            if _is_scopes_present_in_block(func):
                return True

    def _decorate_prog_with_default_torch_scope(prog: Program):
        """
        Decorate every op in the program with a default TORCHSCRIPT_MODULE_TYPE scope info.
        """

        def _decorate_block_with_default_torch_scope(block: Block):
            for op in block.operations:
                for b in op.blocks:
                    _decorate_block_with_default_torch_scope(b)
                assert ScopeSource.TORCHSCRIPT_MODULE_TYPE not in op.scopes
                op.scopes[ScopeSource.TORCHSCRIPT_MODULE_TYPE] = ["dummy"]

        for func in prog.functions.values():
            _decorate_block_with_default_torch_scope(func)

        prog._add_essential_scope_source(ScopeSource.TORCHSCRIPT_MODULE_TYPE)

    if not _is_scopes_present_in_program(prog):
        _decorate_prog_with_default_torch_scope(prog)

def apply_pass_and_basic_check(
    prog: Program,
    pass_name: Union[str, AbstractGraphPass],
    skip_output_name_check: Optional[bool] = False,
    skip_output_type_check: Optional[bool] = False,
    skip_output_shape_check: Optional[bool] = False,
    skip_input_name_check: Optional[bool] = False,
    skip_input_type_check: Optional[bool] = False,
    skip_function_name_check: Optional[bool] = False,
    func_name: Optional[str] = "main",
    skip_essential_scope_check: Optional[bool] = False,
) -> Tuple[Program, Block, Block]:
    """
    Apply pass to the program
    """
    prev_prog = copy.deepcopy(prog)

    graph_pass = pass_name if isinstance(pass_name, AbstractGraphPass) else PASS_REGISTRY[pass_name]

    _decorate_prog_with_scope_if_not_present(prog)
    graph_pass(prog)
    prog.validate(check_essential_scope=not skip_essential_scope_check)

    if not skip_function_name_check:
        if prev_prog.functions.keys() != prog.functions.keys():
            raise ValueError("function names changed during {pass_name}.")

    for name in prev_prog.functions:
        if not skip_output_name_check:
            assert_same_output_names(prev_prog, prog, name)
        if not skip_output_type_check:
            assert_same_output_types(prev_prog, prog, name)
        if not skip_output_shape_check:
            assert_same_output_shapes(prev_prog, prog, name)

        if not skip_input_name_check:
            assert_same_input_names(prev_prog, prog, name)
        if not skip_input_type_check:
            assert_same_input_types(prev_prog, prog, name)

    return prev_prog, prev_prog.functions[func_name], prog.functions[func_name]


def assert_prog_input_type(prog, expected_dtype_str, expected_name=None, index=0):
    block = prog.functions["main"]
    if expected_name is None:
        input_var = list(block.inputs.values())[index]
        assert input_var.is_tensor_or_scalar_of(dtype=expected_dtype_str)
    else:
        for input_var in block.inputs.values():
            if input_var.name == expected_name:
                assert input_var.is_tensor_or_scalar_of(dtype=expected_dtype_str)

def assert_spec_input_type(spec, expected_feature_type, expected_name=None, index=0):
    if expected_name is None:
        assert spec.description.input[index].type.multiArrayType.dataType == expected_feature_type
    else:
        for input in spec.description.input:
            if input.name == expected_name:
                assert input.type.multiArrayType.dataType == expected_feature_type

def assert_input_dtype(mlmodel, expected_type_str, expected_name=None, index=0):
    assert_prog_input_type(
        mlmodel._mil_program, expected_type_str, expected_name=expected_name, index=index
    )
    assert_spec_input_type(
        mlmodel._spec,
        str_to_proto_feature_type(expected_type_str),
        expected_name=expected_name,
        index=index,
    )


def assert_spec_output_type(spec, expected_feature_type, expected_name=None, index=0):
    assert spec.description.output[index].type.multiArrayType.dataType == expected_feature_type
    if expected_name is not None:
        assert spec.description.output[index].name == expected_name

def assert_prog_output_type(prog, expected_dtype_str, expected_name=None, index=0):
    block = prog.functions["main"]
    output_var = block.outputs[index]
    assert output_var.is_tensor_or_scalar_of(dtype=expected_dtype_str)
    if expected_name is not None:
        assert output_var.name == expected_name

def assert_output_dtype(mlmodel, expected_type_str, expected_name=None, index=0):
    assert_prog_output_type(
        mlmodel._mil_program, expected_type_str, expected_name=expected_name, index=index
    )
    assert_spec_output_type(
        mlmodel._spec,
        str_to_proto_feature_type(expected_type_str),
        expected_name=expected_name,
        index=index,
    )


def random_gen_input_feature_type(input_desc):
    if input_desc.type.WhichOneof("Type") == "multiArrayType":
        shape = [s for s in input_desc.type.multiArrayType.shape]
        if (
            input_desc.type.multiArrayType.dataType
            == proto.FeatureTypes_pb2.ArrayFeatureType.FLOAT32
        ):
            dtype = np.float32
        elif (
            input_desc.type.multiArrayType.dataType == proto.FeatureTypes_pb2.ArrayFeatureType.INT32
        ):
            dtype = np.int32
        elif (
            input_desc.type.multiArrayType.dataType
            == proto.FeatureTypes_pb2.ArrayFeatureType.FLOAT16
        ):
            dtype = np.float16
        elif (
            input_desc.type.multiArrayType.dataType
            == proto.FeatureTypes_pb2.ArrayFeatureType.FLOAT64
        ):
            dtype = np.float64
        else:
            raise ValueError("unsupported type")
        return np.random.rand(*shape).astype(dtype)
    elif input_desc.type.WhichOneof("Type") == "imageType":
        if input_desc.type.imageType.colorSpace in (
            proto.FeatureTypes_pb2.ImageFeatureType.BGR,
            proto.FeatureTypes_pb2.ImageFeatureType.RGB,
        ):
            shape = [3, input_desc.type.imageType.height, input_desc.type.imageType.width]
            x = np.random.randint(low=0, high=256, size=shape)
            return Image.fromarray(np.transpose(x, [1, 2, 0]).astype(np.uint8))
        elif (
            input_desc.type.imageType.colorSpace
            == proto.FeatureTypes_pb2.ImageFeatureType.GRAYSCALE
        ):
            shape = [input_desc.type.imageType.height, input_desc.type.imageType.width]
            x = np.random.randint(low=0, high=256, size=shape)
            return Image.fromarray(x.astype(np.uint8), "L")
        elif (
            input_desc.type.imageType.colorSpace
            == proto.FeatureTypes_pb2.ImageFeatureType.GRAYSCALE_FLOAT16
        ):
            shape = (input_desc.type.imageType.height, input_desc.type.imageType.width)
            x = np.random.rand(*shape)
            return Image.fromarray(x.astype(np.float32), 'F')
        else:
            raise ValueError("unrecognized image type")
    else:
        raise ValueError('unsupported type')


def gen_input_shapes_einsum(equation: str, dynamic: bool, backend: Tuple[str, str]):
    equation = equation.replace(" ", "")
    left = equation.split("->")[0]
    var_descs = left.split(",")
    converter_shapes = {}
    shapes = {}
    cur_default_shape = 2
    for symbol in itertools.chain.from_iterable(var_descs):
        if symbol not in shapes:
            shapes[symbol] = cur_default_shape
            if dynamic:
                converter_shapes[symbol] = ct.RangeDim(
                    default=cur_default_shape,
                    upper_bound=cur_default_shape if backend[0] == "mlprogram" else -1,
                )
            else:
                converter_shapes[symbol] = cur_default_shape
            cur_default_shape += 1
    var_shapes = [[shapes[symbol] for symbol in var_desc] for var_desc in var_descs]
    converted_shapes = [ct.TensorType(shape=[converter_shapes[symbol] for symbol in var_desc], dtype=np.float32)
                        for var_desc in var_descs]
    return var_shapes, converted_shapes


def verify_prediction(mlmodel, multiarray_type=None):
    spec = mlmodel._spec
    input_dict = {}
    for input_desc in spec.description.input:
        input_dict[input_desc.name] = random_gen_input_feature_type(input_desc)
        if multiarray_type is not None:
            input_dict[input_desc.name] = input_dict[input].astype(multiarray_type)
    state = mlmodel.make_state() if mlmodel._is_stateful() else None
    res = mlmodel.predict(input_dict, state=state)
    assert isinstance(res, dict)
    assert len(res) == len(spec.description.output)

def assert_spec_input_image_type(spec, expected_feature_type):
    assert spec.description.input[0].type.imageType.colorSpace == expected_feature_type

def assert_spec_output_image_type(spec, expected_feature_type):
    assert spec.description.output[0].type.imageType.colorSpace == expected_feature_type

def assert_cast_ops_count(mlmodel, expected_count):
    block = mlmodel._mil_program.functions["main"]
    assert len(block.find_ops(op_type="cast")) == expected_count

def assert_ops_in_mil_program(mlmodel, expected_op_list):
    assert expected_op_list == get_op_types_in_program(mlmodel._mil_program)


def validate_minimum_deployment_target(
    minimum_deployment_target: ct.target, backend: Tuple[str, str]
):
    """
    Validates the minimum deployment target based on backend and macOS version. Only used in tests.
    """
    if minimum_deployment_target >= ct.target.iOS15 and backend[0] != "mlprogram":
        pytest.skip("IOS15+ target only compatible with mlprogram.")
    if not macos_compatible_with_deployment_target(minimum_deployment_target):
        pytest.skip(
            f"IOS{minimum_deployment_target} target is not runnable on this macOS {coremltoolsutils._macos_version()}"
        )


def compute_snr_and_psnr(x, y):
    assert len(x) == len(y)
    eps = 1e-5
    eps2 = 1e-10
    noise = x - y
    noise_var = np.sum(noise**2) / len(noise)
    signal_energy = np.sum(y**2) / len(y)
    max_signal_energy = np.amax(y**2)
    snr = 10 * np.log10((signal_energy + eps) / (noise_var + eps2))
    psnr = 10 * np.log10((max_signal_energy + eps) / (noise_var + eps2))
    return snr, psnr
