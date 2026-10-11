"""HTTP routes. Thin layer: validation and delegation to the services."""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, Query, Request, UploadFile

from app.config import Settings
from app.models.schemas import ErrorResponse, FileInfo, MeasurementsResponse
from app.services import file_service
from app.services.measurement_service import present_measurements
from app.services.storage_service import StorageService
from app.utils.exceptions import InvalidParameterError, NotFoundError

router = APIRouter(prefix="/api/files", tags=["files"])


def get_storage(request: Request) -> StorageService:
    return request.app.state.storage


def get_app_settings(request: Request) -> Settings:
    return request.app.state.settings


_ERRORS = {
    400: {"model": ErrorResponse, "description": "Empty, corrupted, malformed or unsafe file"},
    413: {"model": ErrorResponse, "description": "File or archive exceeds a configured limit"},
    415: {"model": ErrorResponse, "description": "Unsupported file type"},
    422: {"model": ErrorResponse, "description": "Valid container but unusable dataset"},
}


@router.post(
    "/",
    response_model=FileInfo,
    status_code=201,
    summary="Upload a KML file or a zipped Shapefile",
    responses=_ERRORS,
)
def upload_file(
    file: UploadFile = File(..., description="A .kml file, or a .zip containing one Shapefile."),
    storage: StorageService = Depends(get_storage),
    settings: Settings = Depends(get_app_settings),
) -> FileInfo:
    # Sync endpoint on purpose: parsing/measurement is CPU-bound and FastAPI runs it in a thread pool.
    record = file_service.handle_upload(
        upload_name=file.filename, stream=file.file, storage=storage, settings=settings
    )
    return record.to_info()


@router.get(
    "/{file_id}/",
    response_model=FileInfo,
    summary="Get metadata and processing status of an uploaded file",
    responses={404: {"model": ErrorResponse}},
)
def get_file(file_id: str, storage: StorageService = Depends(get_storage)) -> FileInfo:
    record = storage.get_record(file_id)
    if record is None:
        raise NotFoundError("File not found.", code="FILE_NOT_FOUND")
    return record.to_info()


@router.get(
    "/{file_id}/measurements/",
    response_model=MeasurementsResponse,
    summary="Get per-feature measurements (paginated)",
    responses={404: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
)
def get_measurements(
    file_id: str,
    limit: int | None = Query(None, ge=1, description="Features per page (default and maximum are configurable)."),
    offset: int = Query(0, ge=0, description="Number of features to skip."),
    storage: StorageService = Depends(get_storage),
    settings: Settings = Depends(get_app_settings),
) -> MeasurementsResponse:
    record = storage.get_record(file_id)
    if record is None:
        raise NotFoundError("File not found.", code="FILE_NOT_FOUND")
    page_size = settings.default_page_size if limit is None else limit
    if page_size > settings.max_page_size:
        raise InvalidParameterError(
            f"limit must not exceed {settings.max_page_size}.", code="INVALID_PAGINATION"
        )
    total, features = storage.load_feature_page(file_id, offset, page_size)
    return present_measurements(record, total, features, limit=page_size, offset=offset)
