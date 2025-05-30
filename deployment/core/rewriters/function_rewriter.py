# Copyright (c) OpenMMLab. All rights reserved.
import types
from collections import defaultdict
from typing import (Any, Callable, Dict, List, MutableSequence, Optional,
                    Tuple, Union)

from mmdeploy.utils import IR, Backend, get_root_logger
from .rewriter_utils import (Checker, ContextCaller, RewriterRegistry,
                             copy_function, get_frame_func, get_func_qualname,
                             import_function)

try:
    try:
        # torch>=1.10.0
        from torch.fx._symbolic_trace import _wrapped_fns_to_patch
    except ImportError:
        # 1.10.0>torch>=1.8.0
        from torch.fx.symbolic_trace import _wrapped_fns_to_patch
except ImportError:
    # torch<1.8.0
    _wrapped_fns_to_patch = []


def _replace_all_obj(obj: Any,
                     new_obj: Any,
                     ignore_refs: Tuple[Any] = tuple(),
                     ignore_keys: Tuple[str] = tuple()):
    """Replace all object reference with new_object.

    Args:
        obj (Any): The object to be replaced.
        new_obj (Any): The object to replace obj.
        ignore_refs (Tuple[Any]): These refs will be ignored.
        ignore_keys (Tuple[str]): object with these keys will be ignored.
    """
    import gc
    refs = gc.get_referrers(obj)
    obj_id = id(obj)
    for ref in refs:
        if ref in ignore_refs:
            continue
        elif isinstance(ref, MutableSequence):
            for i, v in enumerate(ref):
                if id(v) == obj_id:
                    ref[i] = new_obj
        elif isinstance(ref, Dict):
            for k, v in ref.items():
                if id(v) == obj_id and k not in ignore_keys:
                    ref[k] = new_obj
        else:
            # TODO: check if we can replace tuple
            pass


def _set_func(origin_func_path: str,
              rewrite_func: Callable,
              ignore_refs: Tuple[Any] = tuple(),
              ignore_keys: Tuple[str] = ('origin_func', )):
    """Rewrite a function by executing a python statement.

    Args:
        origin_func_path (str): The path to origin function.
        rewrite_func (Callable): The new function instance.
        ignore_refs (Tuple[Any]): These refs will be ignored.
        ignore_keys (Tuple[str]): object with these keys will be ignored.
    """

    # Import necessary module
    split_path = origin_func_path.split('.')
    for i in range(len(split_path), 0, -1):
        try:
            exec('import {}'.format('.'.join(split_path[:i])))
            break
        except Exception:
            continue
    origin_func = eval(origin_func_path)
    method_class = False
    if len(split_path) > 1:
        module_or_class = eval('.'.join(split_path[:-1]))
        if isinstance(module_or_class, type):
            method_class = True
    # Assign function
    if not method_class:
        _replace_all_obj(
            origin_func,
            rewrite_func,
            ignore_refs=ignore_refs,
            ignore_keys=ignore_keys)
    exec(f'{origin_func_path} = rewrite_func')


def _del_func(path: str):
    """Delete a function that is denoted by a path.

    Args:
        path (str): The path to evaluate.
    """

    split_path = path.split('.')
    for i in range(len(split_path), 0, -1):
        try:
            exec('import {}'.format('.'.join(split_path[:i])))
            exec(f'del {path}')
            break
        except Exception:
            continue


def _fx_wrap_copied_fn(func: types.FunctionType,
                       copied_func: types.FunctionType):
    """If a function is wrapped by torch.fx.wrap, its copy also needs to be
    wrapped by torch.fx.wrap."""
    if not hasattr(func, '__globals__'):
        return

    wrapped_fns_globals = [item[0] for item in _wrapped_fns_to_patch]
    wrapped_fns_names = [item[1] for item in _wrapped_fns_to_patch]

    # check if wrapped by torch.fx.wrap
    if func.__globals__ in wrapped_fns_globals:
        idx = wrapped_fns_globals.index(func.__globals__)
        fn_name = wrapped_fns_names[idx]
        # a hacky way to wrap the func in copied func
        _wrapped_fns_to_patch.append((copied_func.__globals__, fn_name))


class FunctionRewriter:
    """A function rewriter which maintains rewritten functions.

    The rewritten functions can be registered by calling register_rewriter().
    In RewriteContext, the rewriter automatically replaces target functions and
    recovers them after exiting the context.

    Examples:
        >>> @FUNCTION_REWRITER.register_rewriter(
        >>>     func_name='torch.Tensor.size', backend='ncnn')
        >>> def size_of_tensor_static(self, *args):
        >>>     ctx = FUNCTION_REWRITER.get_context()
        >>>     ret = ctx.origin_func(self, *args)
        >>>     if isinstance(ret, torch.Tensor):
        >>>         ret = int(ret)
        >>>     else:
        >>>         ret = [int(r) for r in ret]
        >>>         ret = tuple(ret)
        >>>     return ret
    """

    def __init__(self):
        self._registry = RewriterRegistry()
        self._func_contexts = defaultdict(list)

    def register_rewriter(
            self,
            func_name: str,
            backend: str = Backend.DEFAULT.value,
            ir: IR = IR.DEFAULT,
            extra_checkers: Optional[Union[Checker, List[Checker]]] = None,
            **kwargs):
        """The interface of function rewriter decorator.

        Args:
            func_name (str): The function name/path to rewrite.
            backend (str): The rewriter will be activated on which backend.
            ir (IR): The rewriter will be activated on which IR.
            extra_checkers (Checker | List[Checker] | None): Other requirements
                defined by Checker.

        Returns:
            Callable: The process of registering function.
        """

        return self._registry.register_object(func_name, backend, ir,
                                              extra_checkers, **kwargs)

    def enter(self, cfg: Dict = dict(), env: Dict = dict(), **kwargs):
        """The implementation of function rewrite."""
        self._func_contexts.clear()
        # Get current records
        functions_records = self._registry.get_records(env)
        # Get current fx wrapped func nums
        self._ori_fx_wrap_num = len(_wrapped_fns_to_patch)

        self._origin_functions = list()
        self._additional_functions = list()
        new_functions = list()
        for function_path, record_dict in functions_records:

            # Check if the origin function exists
            try:
                origin_func, origin_class = import_function(function_path)
            except Exception:
                origin_func = None
                logger = get_root_logger()
                logger.warning(
                    f'Can not find {function_path}, function rewrite will '
                    'not be applied')

            # Only rewrite functions that exist
            if origin_func is not None:

                is_addition_function = False
                if origin_class is not None:
                    function_name = function_path.split('.')[-1]
                    try:
                        origin_class.__getattribute__(origin_class,
                                                      function_name)
                    except Exception:
                        # The function is a method and it is derived from base
                        # class.
                        is_addition_function = True

                if is_addition_function:
                    self._additional_functions.append(function_path)

                # Save origin function
                self._origin_functions.append(
                    dict(func_path=function_path, origin_func=origin_func))

                # Create context_caller
                rewrite_function = record_dict['_object']
                # The func before and after copy has different globals
                rewrite_function = copy_function(rewrite_function)
                extra_kwargs = kwargs.copy()
                extra_kwargs.update(record_dict)
                context_caller = ContextCaller(rewrite_function, origin_func,
                                               cfg, **extra_kwargs)
                # If there is a function wrapped by torch.fx.wrap in
                # rewrite_function's globals, we need to wrap the same name
                # function in copied function's globals.
                _fx_wrap_copied_fn(record_dict['_object'], context_caller.func)

                qualname = get_func_qualname(rewrite_function)
                self._func_contexts[qualname].append(context_caller)
                self._func_contexts[function_path].append(context_caller)

                # Cache new the function to avoid homonymic bug
                new_functions.append(
                    dict(
                        func_path=function_path, origin_func=rewrite_function))

        for func_dict in new_functions:
            function_path = func_dict['func_path']
            new_function = func_dict['origin_func']
            # Rewrite functions
            _set_func(function_path, new_function)

    def exit(self):
        """Recover the function rewrite."""
        # Restore _wrapped_fns_to_patch
        cur_fx_wrap_num = len(_wrapped_fns_to_patch)
        for _ in range(cur_fx_wrap_num - self._ori_fx_wrap_num):
            _wrapped_fns_to_patch.pop(-1)

        for func_dict in self._origin_functions:
            func_path = func_dict['func_path']
            func = func_dict['origin_func']
            _set_func(func_path, func)
        for func_path in self._additional_functions:
            _del_func(func_path)

        self._func_contexts.clear()

    def get_context(self, key: Optional[str] = None) -> ContextCaller:
        """Get the context of rewriter.

        Args:
            key: key to the context.

        Returns:
            ContextCaller: context of function
        """
        func = None
        if key is None:
            func = get_frame_func(2)
            key = get_func_qualname(func)

        # get all contexts
        ctxs = self._func_contexts.get(key, [])

        if func is None:
            assert len(ctxs) == 1
            return ctxs[0]

        ctx = None
        for tmp_ctx in ctxs:
            if tmp_ctx.func == func:
                ctx = tmp_ctx

        if ctx is None:
            get_root_logger().warning(f'Can not found context of {key}')
        return ctx
