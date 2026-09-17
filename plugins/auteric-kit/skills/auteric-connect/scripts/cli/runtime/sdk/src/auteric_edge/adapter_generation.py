"""Generate a review-only Python service adapter from explicit developer bindings.

No repository imports/execution or file changes during generation. A binding is
not inferred from a route's name; decorated route/auth handlers are rejected.
"""
import ast
import hashlib
import json
import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .mapping import Transform
from .models import INPUTS


class ServiceBinding(BaseModel):
    model_config = ConfigDict(extra='forbid')
    operation: str
    module: str = Field(pattern=r'^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*$')
    function: str = Field(pattern=r'^[A-Za-z_]\w*$')
    source_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    request: dict[str, Transform] = Field(default_factory=dict)
    response: dict[str, Transform] = Field(default_factory=dict)
    response_root: str | None = None

    @model_validator(mode='after')
    def canonical(self):
        if self.operation not in INPUTS: raise ValueError('Unknown canonical operation')
        if any(not key.isidentifier() for key in self.request):
            raise ValueError('Request targets must be Python keyword parameter names')
        for key, rule in self.request.items():
            if re.search(r'password|secret|token|api.?key|authorization', key, re.I) and (rule.constant is not None or rule.default is not None):
                raise ValueError('Do not generate credential literals; resolve secrets inside the merchant service')
        return self


class AdapterRecipe(BaseModel):
    model_config = ConfigDict(extra='forbid')
    bindings: list[ServiceBinding] = Field(min_length=1, max_length=11)
    owner_reviewed_service_authentication: bool
    owner_reviewed_no_payment_capture: bool


def generate_adapter(project_root, recipe):
    root = Path(project_root).absolute()
    if not root.is_dir() or any(p.is_symlink() for p in (root, *root.parents)):
        raise ValueError('Choose an existing non-symlink merchant repository')
    recipe = AdapterRecipe.model_validate(recipe)
    if not recipe.owner_reviewed_service_authentication or not recipe.owner_reviewed_no_payment_capture:
        raise ValueError('Explicit service-authentication and no-payment review acknowledgements required')
    if len({b.operation for b in recipe.bindings}) != len(recipe.bindings):
        raise ValueError('Duplicate canonical operation binding')
    sources = []
    for binding in recipe.bindings:
        path = root.joinpath(*binding.module.split('.')).with_suffix('.py')
        if any(p.is_symlink() for p in (path, *path.parents)) or not path.is_file() or path.stat().st_size > 500_000:
            raise ValueError('Bound module must be a bounded local Python source file, not a symlink')
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != binding.source_sha256:
            raise ValueError('Service source differs from the reviewed SHA-256')
        tree = ast.parse(content)
        functions = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == binding.function]
        if len(functions) != 1 or functions[0].decorator_list:
            raise ValueError('Bind an undecorated explicit service function, not an HTTP/authentication handler')
        function = functions[0]
        if function.args.posonlyargs:
            raise ValueError('Positional-only service parameters require an explicit facade')
        names = [a.arg for a in function.args.args]
        optional = len(function.args.defaults)
        required = set(names[:len(names)-optional] if optional else names)
        required.update(a.arg for a, default in zip(function.args.kwonlyargs, function.args.kw_defaults) if default is None)
        accepted = set(names) | {a.arg for a in function.args.kwonlyargs}
        if not required <= set(binding.request) or (not function.args.kwarg and not set(binding.request) <= accepted):
            raise ValueError('Request transformations do not satisfy the service function signature')
        sources.append({'operation': binding.operation, 'path': path.relative_to(root).as_posix(), 'line': function.lineno, 'sha256': binding.source_sha256})
    serialized = recipe.model_dump(mode='json', exclude_unset=True)
    lines = [
        '"""REVIEW BEFORE APPLYING. Merchant-owned adapter; never generated at runtime."""',
        'import asyncio', 'import hashlib', 'import inspect', 'import json', 'from pathlib import Path',
        'from auteric_edge.connector import CommerceConnector',
        'from auteric_edge.mapping import map_fields, lookup',
        'from auteric_edge.models import validate_input, validate_output',
    ]
    for index, binding in enumerate(recipe.bindings):
        lines.append(f'from {binding.module} import {binding.function} as _handler_{index}')
    lines.append('_BINDINGS = json.loads(' + repr(json.dumps([b.model_dump(mode='json', exclude_unset=True) for b in recipe.bindings])) + ')')
    lines.append('_HANDLERS = [' + ', '.join('_handler_' + str(i) for i in range(len(recipe.bindings))) + ']')
    lines.extend([
        'class ReviewedServiceConnector(CommerceConnector):',
        '    supported_operations = frozenset(b["operation"] for b in _BINDINGS)',
        '    def __init__(self):',
        '        self.bindings = {}',
        '        for binding, handler in zip(_BINDINGS, _HANDLERS):',
        '            filename = inspect.getsourcefile(handler)',
        '            if not filename or hashlib.sha256(Path(filename).read_bytes()).hexdigest() != binding["source_sha256"]:',
        '                raise ValueError("Merchant service source changed; review and regenerate the adapter")',
        '            self.bindings[binding["operation"]] = (binding, handler)',
        '    async def _invoke(self, operation, request):',
        '        if operation not in self.bindings:',
        '            raise NotImplementedError("This operation was not reviewed or implemented")',
        '        binding, handler = self.bindings[operation]',
        '        normalized = validate_input(operation, request)',
        '        kwargs = map_fields(binding.get("request", {}), normalized)',
        '        try:',
        '            result = handler(**kwargs) if inspect.iscoroutinefunction(handler) else await asyncio.to_thread(handler, **kwargs)',
        '            if inspect.isawaitable(result): result = await result',
        '        except Exception:',
        '            raise RuntimeError("Merchant service failed; inspect locally before retrying any write") from None',
        '        if binding.get("response_root"): result = lookup(result, binding["response_root"])',
        '        fields = binding.get("response", {})',
        '        if fields:',
        '            result = [map_fields(fields, item) for item in result] if operation == "search_products" else map_fields(fields, result)',
        '        return validate_output(operation, result)',
    ])
    for operation in INPUTS:
        lines.extend([f'    async def {operation}(self, request):', f'        return await self._invoke({operation!r}, request)'])
    lines.extend(['', 'def build_connector():', '    return ReviewedServiceConnector()', ''])
    code = '\n'.join(lines)
    compile(code, 'auteric_connector.py', 'exec')
    return {
        'files': [{'path': 'auteric_connector.py', 'content': code, 'sha256': hashlib.sha256(code.encode()).hexdigest()}],
        'source_bindings': sources, 'reviewed_recipe': serialized,
        'mappings': [{'operation': b.operation, 'mapping': {'kind': 'sdk'}} for b in recipe.bindings],
        'review_required': True, 'application_modified': False, 'execution_verified': False,
        'factory': 'auteric_connector:build_connector',
        'instructions': ['Inspect the generated module before writing it into the merchant repository. Never overwrite an existing file blindly.', 'Only supported_operations may be submitted as Store mappings; the remaining methods fail closed.', 'Run the merchant tests, SDK factory check, exact mapping tests and Connection Test before activation.', 'Imports execute merchant-owned module initialization when the reviewed connector starts; generation itself never imports merchant code.', 'The connector imports only fixed reviewed services. Do not expose raw handler execution directly to agents.'],
    }
