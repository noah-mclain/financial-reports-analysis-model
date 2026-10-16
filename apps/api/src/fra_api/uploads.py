"""Stream a multipart file straight into the same writer used for raw PDF bodies.

Starlette's form parser spools files before returning them and its part-size limit applies
only to text fields. These callbacks instead validate each file chunk before writing it.
The caller runs parser writes in a thread, including the writer's hashing and disk I/O.
"""

from typing import TYPE_CHECKING

from python_multipart import MultipartParser
from python_multipart.exceptions import FormParserError, MultipartParseError
from python_multipart.multipart import parse_options_header

from fra_core.jobs import JobSettings, UploadRejected, UploadTooLarge, UploadWriter

if TYPE_CHECKING:
    from python_multipart.multipart import MultipartCallbacks

PDF_TYPE = "application/pdf"
MULTIPART_TYPE = "multipart/form-data"


class UploadTypeRejected(UploadRejected):
    """The request or file part declares an unsupported media type."""


class MultipartUpload:
    def __init__(self, writer: UploadWriter, content_type: str, settings: JobSettings) -> None:
        _, options = parse_options_header(content_type)
        boundary = options.get(b"boundary")
        if not boundary:
            raise UploadRejected("missing multipart boundary")
        self._writer = writer
        self._limit = settings.max_multipart_overhead_bytes
        self._part_limit = settings.max_multipart_parts
        self._envelope_limit = settings.max_upload_bytes + self._limit
        self._envelope_bytes = 0
        self._parts = 0
        self._metadata_bytes = 0
        self._headers: dict[bytes, bytes] = {}
        self._header_name = bytearray()
        self._header_value = bytearray()
        self._is_file = False
        self._files = 0
        self._filename: str | None = None
        self._complete = False
        callbacks: MultipartCallbacks = {
            "on_part_begin": self._part_begin,
            "on_header_field": self._header_field,
            "on_header_value": self._header_data,
            "on_header_end": self._header_end,
            "on_headers_finished": self._headers_finished,
            "on_part_data": self._part_data,
            "on_end": self._end,
        }
        try:
            self._parser = MultipartParser(boundary, callbacks)
        except FormParserError as exc:
            raise UploadRejected(f"invalid multipart boundary: {exc}") from exc

    def _metadata(self, size: int) -> None:
        # Bound ignored text fields and accumulated headers as well as the file itself.
        self._metadata_bytes += size
        if self._metadata_bytes > self._limit:
            raise UploadTooLarge(f"multipart metadata passes the limit of {self._limit} bytes")

    def _part_begin(self) -> None:
        self._parts += 1
        if self._parts > self._part_limit:
            raise UploadRejected(f"multipart parts pass the limit of {self._part_limit}")
        self._headers = {}
        self._is_file = False

    def _header_field(self, data: bytes, start: int, end: int) -> None:
        self._metadata(end - start)
        self._header_name.extend(data[start:end])

    def _header_data(self, data: bytes, start: int, end: int) -> None:
        self._metadata(end - start)
        self._header_value.extend(data[start:end])

    def _header_end(self) -> None:
        name = bytes(self._header_name).lower()
        if name in self._headers:
            raise UploadRejected(f"duplicate multipart header {name.decode('latin-1')}")
        self._headers[name] = bytes(self._header_value)
        self._header_name.clear()
        self._header_value.clear()

    def _headers_finished(self) -> None:
        disposition, options = parse_options_header(self._headers.get(b"content-disposition", b""))
        if disposition != b"form-data" or b"name" not in options:
            raise UploadRejected("multipart part requires form-data disposition and a name")
        self._is_file = b"filename" in options
        if not self._is_file:
            return
        self._files += 1
        if self._files > 1:
            raise UploadRejected("more than one file part")
        if options[b"name"] != b"file":
            raise UploadRejected("missing file part 'file': unexpected file field")
        filename = options[b"filename"]
        if not filename:
            raise UploadRejected("file part requires a filename")
        try:
            self._filename = filename.decode("utf-8")
        except UnicodeDecodeError:
            self._filename = filename.decode("latin-1")
        content_type, _ = parse_options_header(self._headers.get(b"content-type", b""))
        if content_type != PDF_TYPE.encode():
            raise UploadTypeRejected(f"file content type must be {PDF_TYPE}")

    def _part_data(self, data: bytes, start: int, end: int) -> None:
        if self._is_file:
            self._writer.write(data[start:end])
        else:
            self._metadata(end - start)

    def _end(self) -> None:
        self._complete = True

    def write(self, data: bytes) -> None:
        # Count the entire received envelope, even bytes the parser ignores after on_end.
        self._envelope_bytes += len(data)
        if self._envelope_bytes > self._envelope_limit:
            raise UploadTooLarge(
                f"multipart envelope passes the limit of {self._envelope_limit} bytes"
            )
        try:
            self._parser.write(data)
        except MultipartParseError as exc:
            raise UploadRejected(f"malformed multipart: {exc}") from exc

    def filename(self) -> str:
        # python-multipart's finalize does not check truncated input; on_end must fire.
        if not self._complete:
            raise UploadRejected("incomplete multipart body")
        if self._filename is None:
            raise UploadRejected("missing file part 'file'")
        return self._filename
