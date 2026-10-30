"""Authenticated, portfolio-independent generic format downloads."""

import json
from typing import Annotated

from fastapi import APIRouter, Depends, Response, status

from app.core.constants import GENERIC_IMPORT_SCHEMA_FILENAME, GENERIC_IMPORT_TEMPLATE_FILENAME, GENERIC_IMPORT_URL_PREFIX
from app.core.deps import get_current_user
from app.services.generic_import_format_service import GenericImportFormatService

router = APIRouter(prefix=GENERIC_IMPORT_URL_PREFIX, tags=["imports"], dependencies=[Depends(get_current_user)])


def _get_service() -> GenericImportFormatService:
    """Inject the stateless reference-format service."""
    return GenericImportFormatService()


@router.get(
    "/schema",
    # Raw file download, not a JSON response-envelope resource.
    response_model=None,
    response_class=Response,
    status_code=status.HTTP_200_OK,
    summary="Download the generic execution v1 JSON Schema",
    description=(
        "Download the JSON Schema generated from the generic v1 import document model, including execution and corporate-action definitions. "
        "It describes case-insensitive enum inputs, numeric multiplier strings and conditional derivative/order requirements. "
        "OCC-field agreement and cross-record/history checks remain importer validation rules. "
        "The response is a raw UTF-8 JSON Schema attachment, not an import document or a response envelope. "
        "A valid Bearer token is required; no account, portfolio selection or portfolio ownership is required."
    ),
    response_description="The generated JSON Schema file, returned directly as application/schema+json.",
    responses={
        status.HTTP_200_OK: {
            "description": "Raw JSON Schema attachment; response_model=None is intentional for this file download.",
            "content": {"application/schema+json": {"schema": {"type": "string", "format": "binary"}}},
            "headers": {
                "Content-Disposition": {
                    "description": "Attachment filename for the versioned JSON Schema.",
                    "schema": {"type": "string"},
                    "example": f'attachment; filename="{GENERIC_IMPORT_SCHEMA_FILENAME}"',
                }
            },
        },
        status.HTTP_401_UNAUTHORIZED: {
            "description": "Missing, expired or invalid Bearer token, or inactive user.",
            "headers": {"WWW-Authenticate": {"description": "Bearer authentication challenge.", "schema": {"type": "string"}, "example": "Bearer"}},
        },
    },
)
async def get_generic_import_schema(
    service: Annotated[GenericImportFormatService, Depends(_get_service)],
) -> Response:
    """Download the generated schema without requiring a portfolio scope.

    Args:
        service: Injected service supplying the versioned schema.

    Returns:
        A UTF-8 JSON Schema attachment using application/schema+json and
        Content-Disposition, without a response envelope. This file response
        deliberately uses response_model=None rather than a document DTO.

    Raises:
        HTTPException: 401 if the authentication dependency rejects the token
            or user. No portfolio ownership check is performed.
    """
    return Response(
        content=json.dumps(service.get_json_schema()).encode("utf-8"),
        media_type="application/schema+json",
        headers={"Content-Disposition": f'attachment; filename="{GENERIC_IMPORT_SCHEMA_FILENAME}"'},
    )


@router.get(
    "/template.csv",
    # Raw CSV attachment, not a JSON response-envelope resource.
    response_model=None,
    response_class=Response,
    status_code=status.HTTP_200_OK,
    summary="Download the generic execution v1 CSV template",
    description=(
        "Download a UTF-8 CSV attachment whose header contains every v1 execution field. "
        "Synthetic examples include an equity round trip and an option spread with a shared order_id, "
        "distinct instrument_id values and consistent OPEN markers. The template can be imported into an empty generic account. "
        "The response is raw CSV, not a response envelope. A valid Bearer token is required; "
        "no account, portfolio selection or portfolio ownership is required."
    ),
    response_description="The importable CSV file, returned directly as text/csv; charset=utf-8.",
    responses={
        status.HTTP_200_OK: {
            "description": "Raw UTF-8 CSV attachment; response_model=None is intentional for this file download.",
            "content": {"text/csv": {"schema": {"type": "string", "format": "binary"}}},
            "headers": {
                "Content-Disposition": {
                    "description": "Attachment filename for the versioned CSV template.",
                    "schema": {"type": "string"},
                    "example": f'attachment; filename="{GENERIC_IMPORT_TEMPLATE_FILENAME}"',
                }
            },
        },
        status.HTTP_401_UNAUTHORIZED: {
            "description": "Missing, expired or invalid Bearer token, or inactive user.",
            "headers": {"WWW-Authenticate": {"description": "Bearer authentication challenge.", "schema": {"type": "string"}, "example": "Bearer"}},
        },
    },
)
async def get_generic_import_template(
    service: Annotated[GenericImportFormatService, Depends(_get_service)],
) -> Response:
    """Download the CSV template without requiring a portfolio scope.

    Args:
        service: Injected service building the versioned CSV reference.

    Returns:
        A text/csv; charset=utf-8 attachment containing synthetic executions
        and Content-Disposition, without a response envelope. This file
        response deliberately uses response_model=None.

    Raises:
        HTTPException: 401 if the authentication dependency rejects the token
            or user. No portfolio ownership check is performed.
    """
    return Response(
        content=service.get_csv_template().encode("utf-8"),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{GENERIC_IMPORT_TEMPLATE_FILENAME}"'},
    )
