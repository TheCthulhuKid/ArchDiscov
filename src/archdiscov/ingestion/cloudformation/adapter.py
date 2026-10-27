"""Complete snapshots from offline declarations; no stack or provider execution."""

from ...models.json_pointer import escape_pointer_token
from ..adapter import AdapterContext, IngestionError
from .context import parameters_for, prepare_template, pseudo_parameters, safe_properties
from .evaluation import evaluate
from .normalization import normalize_resource
from .parsing import load_template
from .relationships import link, place_resources, qualify_children
from .requests import CloudFormationRequest
from .snapshot import SnapshotBuilder


class CloudFormationAdapter:
    def collect(self, request, *, context: AdapterContext):
        if not isinstance(request, CloudFormationRequest):
            raise IngestionError("invalid_request", "Expected a CloudFormationRequest")
        template = prepare_template(load_template(request.path), request)
        builder = SnapshotBuilder(request, context)
        parameters = parameters_for(template, request)
        options = {
            "parameters": parameters,
            "resources": set(template["Resources"]),
            "pseudo": pseudo_parameters(request),
            "mappings": safe_properties(template.get("Mappings", {}))[0],
            "conditions": template.get("Conditions", {}),
        }
        references = {}
        for logical_id, entry in template["Resources"].items():
            path = f"/Resources/{escape_pointer_token(logical_id)}"
            condition = entry.get("Condition")
            if condition is not None:
                evaluated = evaluate({"Condition": condition}, **options)
                if evaluated.value is False:
                    continue
                if evaluated.value is not True:
                    builder.omit(path + "/Condition", "Resource condition could not be resolved")
                    continue
            references[logical_id] = normalize_resource(builder, logical_id, entry, options)
        for logical_id, resource in builder.resources.items():
            path = f"/Resources/{escape_pointer_token(logical_id)}"
            for ref in references[logical_id]:
                link(
                    builder,
                    resource,
                    path + "/Properties" + ref.path,
                    ref.logical_id,
                    attribute=ref.attribute,
                )
            entry = template["Resources"][logical_id]
            depends_on = entry.get("DependsOn", [])
            for index, target in enumerate(
                [depends_on] if isinstance(depends_on, str) else depends_on
            ):
                link(
                    builder,
                    resource,
                    path + "/DependsOn" + ("" if isinstance(depends_on, str) else f"/{index}"),
                    target,
                )
        if "Transform" in template:
            builder.omit("/Transform", "Template transforms and macros are not executed")
        if "Rules" in template:
            builder.omit("/Rules", "Template parameter rules are not evaluated")
        qualify_children(builder, template, references)
        regions, globals_ = place_resources(builder, references)
        return [builder.finish(template, regions, globals_)]
