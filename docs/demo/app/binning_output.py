"""
binning_output.py
=================
Write the binned table (input columns + opt_<var>_woe / opt_<var>_group /
opt_<var>_label) produced by the binning webapp to a managed Dataiku dataset.

Connection-agnostic: when the output dataset does not exist yet it is created
as a managed dataset on the *same connection as the input dataset* (Snowflake
in -> Snowflake out, S3 in -> S3 out, ...). An existing managed dataset is
overwritten in place, schema included.

The output is the input of the Scorecard Scaling Optimization webapp, which
looks for opt_<var>_woe + opt_<var>_group columns and the target column.
"""

import re

import dataiku

# Fallback connection when the input's connection cannot host a managed
# dataset (e.g. a read-only source).
FALLBACK_CONNECTIONS = ("filesystem_managed",)
_NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")


def _project():
    return dataiku.api_client().get_default_project()


def _connection_of(raw):
    params = raw.get("params") or {}
    return params.get("connection") or params.get("uploadConnection")


def _create_managed(project, name, connection):
    helper = project.new_managed_dataset(name)
    helper.with_store_into(connection)
    return helper.create()


def write_output_dataset(input_name, output_name, df, source_webapp="Binning Optimization v2", description=None):
    """Create (if needed) and overwrite ``output_name`` with ``df``.

    Returns a dict describing where the data went.
    """
    if not output_name or not _NAME_RE.match(output_name):
        raise ValueError("Output dataset name must use only letters, digits and underscores.")
    if output_name == input_name:
        raise ValueError("Output dataset must differ from the input dataset.")

    project = _project()
    input_raw = project.get_dataset(input_name).get_settings().get_raw()
    input_connection = _connection_of(input_raw)

    existing = {d["name"] for d in project.list_datasets()}
    created = False
    if output_name in existing:
        out_raw = project.get_dataset(output_name).get_settings().get_raw()
        if not out_raw.get("managed", False):
            raise ValueError(f"Dataset '{output_name}' exists and is not a managed dataset; "
                             "pick another name so it isn't overwritten.")
    else:
        errors = []
        for conn in [input_connection, *FALLBACK_CONNECTIONS]:
            if not conn:
                continue
            try:
                _create_managed(project, output_name, conn)
                created = True
                break
            except Exception as e:  # try the next candidate connection
                errors.append(f"{conn}: {e}")
        if not created:
            raise RuntimeError("Could not create output dataset. " + " | ".join(errors))

    settings = project.get_dataset(output_name).get_settings()
    out_raw = settings.get_raw()
    out_raw["shortDesc"] = description or (f"Binned (WOE/group/label) version of {input_name}, "
                                           f"written by the {source_webapp} webapp.")
    settings.save()

    dataiku.Dataset(output_name).write_with_schema(df)

    return dict(
        created=created,
        connection=_connection_of(out_raw),
        dataset_type=out_raw.get("type"),
        input_connection=input_connection,
        n_columns=int(df.shape[1]),
    )
