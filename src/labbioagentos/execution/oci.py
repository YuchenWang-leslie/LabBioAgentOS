"""A small generic OCI Distribution client and OCI layout reader (DECIDED-C15-A, H).

Only the OCI Distribution specification is used: manifests and blobs by digest,
the image index, and basic or bearer-token authentication. No registry vendor
API is called, so one code path serves every conforming registry. Every byte
is checked against its digest, and the image is written as an OCI image layout
that Docker loads and Apptainer converts without network access.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat
import tarfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

import httpx

from .identity import SourceIdentity, SourceKind

INDEX_TYPES = ("application/vnd.oci.image.index.v1+json",
               "application/vnd.docker.distribution.manifest.list.v2+json")
MANIFEST_TYPES = ("application/vnd.oci.image.manifest.v1+json",
                  "application/vnd.docker.distribution.manifest.v2+json")
_REFERENCE = re.compile(
    r"(?P<host>[a-z0-9]+(?:[.-][a-z0-9]+)*(?::[0-9]{1,5})?)/"
    r"(?P<name>[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*(?:/[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*)*)"
    r"@(?P<digest>sha256:[0-9a-f]{64})"
)
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_MAX_DOCUMENT_BYTES = 4_194_304
# The one naming convention every OCI client shares: docker.io names are served elsewhere.
_REGISTRY_ENDPOINTS = {"docker.io": "registry-1.docker.io"}
MACHINE_PLATFORMS = {"x86_64": "linux/amd64", "aarch64": "linux/arm64", "ppc64le": "linux/ppc64le",
                     "s390x": "linux/s390x"}


class OciError(ValueError):
    """A fixed, value-free reason an image could not be acquired."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class OciReference:
    host: str
    name: str
    digest: str

    @classmethod
    def parse(cls, text: str) -> "OciReference":
        match = _REFERENCE.fullmatch(text)
        # The first component is a registry host only if it looks like one; a bare
        # "lab/tools" would otherwise send the pull to a host named "lab".
        host = match.group("host") if match else ""
        if match is None or not ("." in host or ":" in host or host == "localhost"):
            raise OciError("REFERENCE_NOT_PINNED")  # host/name@sha256:<64 hex> only.
        return cls(**match.groupdict())

    @property
    def endpoint(self) -> str:
        host = _REGISTRY_ENDPOINTS.get(self.host, self.host)
        loopback = host.split(":")[0] in {"localhost", "127.0.0.1"}
        return f"{'http' if loopback else 'https'}://{host}"


@dataclass(frozen=True)
class RegistryCredential:
    username: str
    secret: str


@dataclass(frozen=True)
class AcquiredImage:
    """An OCI layout on disk holding exactly one verified platform manifest."""

    layout: Path
    source: SourceIdentity
    size_bytes: int
    architecture: str


def platform_for(machine: str) -> str:
    try:
        return MACHINE_PLATFORMS[machine]
    except KeyError:
        raise OciError("PLATFORM_UNSUPPORTED") from None


class OciRegistryClient:
    def __init__(self, *, transport: httpx.BaseTransport | None = None,
                 credentials: dict[str, RegistryCredential] | None = None, timeout: float = 120.0):
        self.credentials = credentials or {}
        # Blobs are commonly redirected to storage hosts; httpx drops Authorization
        # on a cross-origin redirect, and every redirect must stay on https.
        self.client = httpx.Client(transport=transport, follow_redirects=True, timeout=timeout,
                                   trust_env=True, event_hooks={"response": [self._https_only]})
        self._tokens: dict[str, str] = {}

    def close(self) -> None:
        self.client.close()

    @staticmethod
    def _https_only(response: httpx.Response) -> None:
        for item in (*response.history, response):
            url = item.request.url
            if url.scheme != "https" and url.host not in {"localhost", "127.0.0.1"}:
                raise OciError("REGISTRY_NOT_HTTPS")

    def pull(self, reference: OciReference, *, platform: str, layout: Path, max_bytes: int) -> AcquiredImage:
        """Fetch one platform image by digest into a new, empty OCI layout directory."""
        accept = ", ".join((*INDEX_TYPES, *MANIFEST_TYPES))
        body, media_type = self._document(reference, reference.digest, accept)
        manifest_digest = reference.digest
        if media_type in INDEX_TYPES:
            manifest_digest = select_platform(json.loads(body), platform)
            body, media_type = self._document(reference, manifest_digest, ", ".join(MANIFEST_TYPES))
        if media_type not in MANIFEST_TYPES:
            raise OciError("MANIFEST_UNSUPPORTED")
        manifest = json.loads(body)
        blobs = _blobs(manifest)
        size = sum(item["size"] for item in blobs) + len(body)
        if size > max_bytes:
            raise OciError("IMAGE_TOO_LARGE")
        _new_layout(layout)
        _write_blob(layout, manifest_digest, body)
        for blob in blobs:
            self._download(reference, blob, layout)
        architecture = _check_config(layout, manifest, platform)
        _write_index(layout, manifest_digest, media_type, len(body))
        source = SourceIdentity(kind=SourceKind.OCI_MANIFEST, digest=manifest_digest, platform=platform)
        return AcquiredImage(layout, source, size, architecture)

    def _document(self, reference: OciReference, digest: str, accept: str) -> tuple[bytes, str]:
        response = self._get(reference, f"/v2/{reference.name}/manifests/{digest}", {"Accept": accept})
        body = response.content
        if len(body) > _MAX_DOCUMENT_BYTES:
            raise OciError("MANIFEST_TOO_LARGE")
        if "sha256:" + hashlib.sha256(body).hexdigest() != digest:
            raise OciError("DIGEST_MISMATCH")
        media_type = response.headers.get("content-type", "").split(";")[0].strip()
        try:
            declared = json.loads(body).get("mediaType")
        except (ValueError, AttributeError):
            raise OciError("MANIFEST_UNSUPPORTED") from None
        return body, declared or media_type

    def _download(self, reference: OciReference, blob: dict, layout: Path) -> None:
        path = layout / "blobs" / "sha256" / blob["digest"].split(":")[1]
        if path.exists():
            return  # The same blob listed twice is written once; its digest was checked.
        digest, written = hashlib.sha256(), 0
        temporary = path.with_name(path.name + ".partial")
        with self._stream(reference, f"/v2/{reference.name}/blobs/{blob['digest']}") as response, \
                open(temporary, "xb") as output:
            for chunk in response.iter_bytes():
                written += len(chunk)
                if written > blob["size"]:
                    raise OciError("BLOB_SIZE_MISMATCH")
                digest.update(chunk)
                output.write(chunk)
        if written != blob["size"] or "sha256:" + digest.hexdigest() != blob["digest"]:
            temporary.unlink()
            raise OciError("DIGEST_MISMATCH")
        temporary.rename(path)

    def _get(self, reference: OciReference, path: str, headers: dict) -> httpx.Response:
        response = self._send(reference, path, headers)
        response.read()
        return response

    def _stream(self, reference: OciReference, path: str):
        response = self._send(reference, path, {})
        return _Closing(response)

    def _send(self, reference: OciReference, path: str, headers: dict) -> httpx.Response:
        url = reference.endpoint + path
        for attempt in range(2):
            request = self.client.build_request("GET", url, headers={**headers, **self._auth(reference)})
            try:
                response = self.client.send(request, stream=True)
            except httpx.HTTPError:
                raise OciError("REGISTRY_UNREACHABLE") from None
            if response.status_code == 401 and attempt == 0:
                challenge = response.headers.get("www-authenticate", "")
                response.close()
                self._authenticate(reference, challenge)
                continue
            if response.status_code == 200:
                return response
            response.close()
            raise OciError({401: "REGISTRY_UNAUTHORIZED", 403: "REGISTRY_UNAUTHORIZED",
                            404: "IMAGE_NOT_FOUND", 429: "REGISTRY_RATE_LIMITED"}.get(
                                response.status_code, "REGISTRY_ERROR"))
        raise OciError("REGISTRY_UNAUTHORIZED")

    def _auth(self, reference: OciReference) -> dict:
        token = self._tokens.get(reference.host)
        if token is not None:
            return {"Authorization": token}
        return {}

    def _authenticate(self, reference: OciReference, challenge: str) -> None:
        credential = self.credentials.get(reference.host)
        scheme, _, parameters = challenge.partition(" ")
        if scheme.lower() == "basic":
            if credential is None:
                raise OciError("REGISTRY_UNAUTHORIZED")
            self._tokens[reference.host] = _basic(credential)
            return
        if scheme.lower() != "bearer":
            raise OciError("REGISTRY_UNAUTHORIZED")
        fields = dict(re.findall(r'(\w+)="([^"]*)"', parameters))
        realm = fields.get("realm", "")
        if urlsplit(realm).scheme != "https" and urlsplit(realm).hostname not in {"localhost", "127.0.0.1"}:
            raise OciError("REGISTRY_NOT_HTTPS")
        params = {"scope": f"repository:{reference.name}:pull"}
        if "service" in fields:
            params["service"] = fields["service"]
        headers = {"Authorization": _basic(credential)} if credential else {}
        try:
            response = self.client.get(realm, params=params, headers=headers)
        except httpx.HTTPError:
            raise OciError("REGISTRY_UNREACHABLE") from None
        if response.status_code != 200:
            raise OciError("REGISTRY_UNAUTHORIZED")
        payload = response.json()
        token = payload.get("token") or payload.get("access_token")
        if not isinstance(token, str) or not token:
            raise OciError("REGISTRY_UNAUTHORIZED")
        self._tokens[reference.host] = f"Bearer {token}"


class _Closing:
    def __init__(self, response: httpx.Response):
        self.response = response

    def __enter__(self) -> httpx.Response:
        return self.response

    def __exit__(self, *_exc) -> None:
        self.response.close()


def _basic(credential: RegistryCredential) -> str:
    pair = f"{credential.username}:{credential.secret}".encode("utf-8")
    return "Basic " + base64.b64encode(pair).decode("ascii")


def select_platform(index: dict, platform: str) -> str:
    """The one manifest for this platform; none or several is an error, never a guess."""
    os_name, _, architecture = platform.partition("/")
    matches = [item for item in index.get("manifests", ())
               if isinstance(item, dict) and isinstance(item.get("platform"), dict)
               and item["platform"].get("os") == os_name
               and item["platform"].get("architecture") == architecture
               and item.get("mediaType", MANIFEST_TYPES[0]) in MANIFEST_TYPES]
    if len(matches) != 1 or not isinstance(matches[0].get("digest"), str) \
            or _DIGEST.fullmatch(matches[0]["digest"]) is None:
        raise OciError("PLATFORM_NOT_AVAILABLE" if not matches else "PLATFORM_AMBIGUOUS")
    return matches[0]["digest"]


def _blobs(manifest: dict) -> list[dict]:
    blobs = [manifest.get("config"), *manifest.get("layers", ())]
    for blob in blobs:
        if (not isinstance(blob, dict) or not isinstance(blob.get("digest"), str)
                or _DIGEST.fullmatch(blob["digest"]) is None
                or type(blob.get("size")) is not int or blob["size"] < 0):
            raise OciError("MANIFEST_UNSUPPORTED")
    return blobs


def _new_layout(layout: Path) -> None:
    layout.mkdir(mode=0o700)
    (layout / "blobs" / "sha256").mkdir(parents=True, mode=0o700)
    (layout / "oci-layout").write_text(json.dumps({"imageLayoutVersion": "1.0.0"}))


def _write_blob(layout: Path, digest: str, body: bytes) -> None:
    (layout / "blobs" / "sha256" / digest.split(":")[1]).write_bytes(body)


def _write_index(layout: Path, digest: str, media_type: str, size: int) -> None:
    index = {"schemaVersion": 2, "mediaType": INDEX_TYPES[0],
             "manifests": [{"mediaType": media_type, "digest": digest, "size": size}]}
    (layout / "index.json").write_text(json.dumps(index))


def _check_config(layout: Path, manifest: dict, platform: str) -> str:
    path = layout / "blobs" / "sha256" / manifest["config"]["digest"].split(":")[1]
    try:
        config = json.loads(path.read_bytes())
    except ValueError:
        raise OciError("MANIFEST_UNSUPPORTED") from None
    os_name, _, architecture = platform.partition("/")
    # The image's own statement of where it runs must match the compute nodes.
    if config.get("os") != os_name or config.get("architecture") != architecture:
        raise OciError("ARCHITECTURE_MISMATCH")
    return architecture


def read_archive(archive: Path, *, platform: str, layout: Path, max_bytes: int) -> AcquiredImage:
    """Unpack an OCI layout archive safely and verify it as a pulled image would be."""
    if archive.is_symlink() or not archive.is_file() or archive.stat().st_size > max_bytes:
        raise OciError("ARCHIVE_INVALID" if not archive.is_file() else "IMAGE_TOO_LARGE")
    staging = layout.with_name(layout.name + ".unpack")
    staging.mkdir(mode=0o700)
    try:
        with tarfile.open(archive, "r:") as tar:
            for member in tar:
                path = PurePosixPath(member.name)
                if (path.is_absolute() or ".." in path.parts or not (member.isfile() or member.isdir())):
                    raise OciError("ARCHIVE_INVALID")  # No links, devices or escapes.
                if member.isdir():
                    continue
                if path.parts[0] not in {"blobs", "index.json", "oci-layout", "manifest.json", "repositories"}:
                    raise OciError("ARCHIVE_INVALID")
                target = staging.joinpath(*path.parts)
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                source = tar.extractfile(member)
                with open(target, "xb") as output:
                    output.write(source.read())
        index = json.loads((staging / "index.json").read_bytes())
        manifest_digest, media_type = _layout_manifest(staging, index, platform)
        body = (staging / "blobs" / "sha256" / manifest_digest.split(":")[1]).read_bytes()
        _verify_file(staging, manifest_digest)
        manifest = json.loads(body)
        blobs = _blobs(manifest)
        size = sum(item["size"] for item in blobs) + len(body)
        if size > max_bytes:
            raise OciError("IMAGE_TOO_LARGE")
        _new_layout(layout)
        _write_blob(layout, manifest_digest, body)
        for blob in blobs:
            _verify_file(staging, blob["digest"], blob["size"])
            name = blob["digest"].split(":")[1]
            os.replace(staging / "blobs" / "sha256" / name, layout / "blobs" / "sha256" / name)
        architecture = _check_config(layout, manifest, platform)
        _write_index(layout, manifest_digest, media_type, len(body))
    except (OSError, ValueError, KeyError, tarfile.TarError) as exc:
        if isinstance(exc, OciError):
            raise
        raise OciError("ARCHIVE_INVALID") from None
    finally:
        _remove_tree(staging)
    source = SourceIdentity(kind=SourceKind.OCI_MANIFEST, digest=manifest_digest, platform=platform)
    return AcquiredImage(layout, source, size, architecture)


def _layout_manifest(staging: Path, index: dict, platform: str) -> tuple[str, str]:
    entries = index.get("manifests", ())
    if len(entries) == 1 and entries[0].get("mediaType") in INDEX_TYPES:
        nested = entries[0]["digest"]
        _verify_file(staging, nested)
        index = json.loads((staging / "blobs" / "sha256" / nested.split(":")[1]).read_bytes())
        entries = index.get("manifests", ())
    if len(entries) == 1 and "platform" not in entries[0]:
        digest = entries[0].get("digest", "")
        if _DIGEST.fullmatch(digest) is None:
            raise OciError("ARCHIVE_INVALID")
        return digest, entries[0].get("mediaType", MANIFEST_TYPES[0])
    digest = select_platform(index, platform)
    media_type = next(item.get("mediaType", MANIFEST_TYPES[0]) for item in entries if item.get("digest") == digest)
    return digest, media_type


def _verify_file(root: Path, digest: str, size: int | None = None) -> None:
    path = root / "blobs" / "sha256" / digest.split(":")[1]
    if not path.is_file() or path.is_symlink() or (size is not None and path.stat().st_size != size):
        raise OciError("ARCHIVE_INVALID")
    hasher = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1_048_576), b""):
            hasher.update(chunk)
    if "sha256:" + hasher.hexdigest() != digest:
        raise OciError("DIGEST_MISMATCH")


def _remove_tree(root: Path) -> None:
    if not root.exists():
        return
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if path.is_dir() and not path.is_symlink():
            path.rmdir()
        else:
            path.unlink()
    root.rmdir()


def layout_archive(layout: Path, archive: Path) -> None:
    """A tar of the layout, the form `docker load` accepts."""
    with tarfile.open(archive, "x:") as tar:
        for path in sorted(layout.rglob("*")):
            info = tar.gettarinfo(path, arcname=str(path.relative_to(layout)))
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            if stat.S_ISREG(info.mode):
                with open(path, "rb") as stream:
                    tar.addfile(info, stream)
            else:
                tar.addfile(info)
