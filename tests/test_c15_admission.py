"""C15 step 5a: the generic OCI client and admitting personal images (DECIDED-C15-A, H)."""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
from pathlib import Path

import httpx
import pytest

from labbioagentos.execution.admission import (
    AdmissionError,
    AdmissionLimits,
    ApptainerRealizer,
    ImageAdmission,
    Realized,
    Realizer,
    load_registry_credentials,
    parse_probe,
)
from labbioagentos.execution.apptainer import SifStore, SifStoreChain, SifStoreError
from labbioagentos.execution.catalog import CatalogStore, ExecutionBackendName, GpuSupport, ImageScope
from labbioagentos.execution.identity import SourceKind
from labbioagentos.execution.inventory import InventoryOrigin
from labbioagentos.execution.oci import (
    INDEX_TYPES,
    MANIFEST_TYPES,
    OciError,
    OciReference,
    OciRegistryClient,
    RegistryCredential,
    layout_archive,
    read_archive,
)

HOST = "registry.example"


def _digest(body: bytes) -> str:
    return "sha256:" + hashlib.sha256(body).hexdigest()


class FakeRegistry:
    """A conforming registry in memory: one image index with one platform per entry."""

    def __init__(self, platforms=(("linux", "amd64"),), config_arch=None, auth=None):
        self.blobs, self.manifests, self.requests = {}, {}, []
        self.auth, self.token_requests = auth, []
        entries = []
        for os_name, arch in platforms:
            layer = f"layer for {arch}".encode()
            config = json.dumps({"os": os_name, "architecture": config_arch or arch}).encode()
            for body in (layer, config):
                self.blobs[_digest(body)] = body
            manifest = json.dumps({
                "schemaVersion": 2, "mediaType": MANIFEST_TYPES[0],
                "config": {"mediaType": "application/vnd.oci.image.config.v1+json",
                           "digest": _digest(config), "size": len(config)},
                "layers": [{"mediaType": "application/vnd.oci.image.layer.v1.tar",
                            "digest": _digest(layer), "size": len(layer)}],
            }).encode()
            self.manifests[_digest(manifest)] = (manifest, MANIFEST_TYPES[0])
            entries.append({"mediaType": MANIFEST_TYPES[0], "digest": _digest(manifest),
                            "size": len(manifest), "platform": {"os": os_name, "architecture": arch}})
        index = json.dumps({"schemaVersion": 2, "mediaType": INDEX_TYPES[0], "manifests": entries}).encode()
        self.index_digest = _digest(index)
        self.manifests[self.index_digest] = (index, INDEX_TYPES[0])
        self.platform_manifest = entries[0]["digest"]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "auth.example":
            self.token_requests.append(request)
            return httpx.Response(200, json={"token": "issued-token"})
        if self.auth and request.headers.get("authorization") != "Bearer issued-token":
            return httpx.Response(401, headers={"www-authenticate":
                'Bearer realm="https://auth.example/token",service="registry.example"'})
        path = request.url.path
        reference = path.rsplit("/", 1)[1]
        if "/manifests/" in path and reference in self.manifests:
            body, media_type = self.manifests[reference]
            return httpx.Response(200, content=body, headers={"content-type": media_type})
        if "/blobs/" in path and reference in self.blobs:
            return httpx.Response(200, content=self.blobs[reference])
        return httpx.Response(404)

    def client(self, **kwargs) -> OciRegistryClient:
        return OciRegistryClient(transport=httpx.MockTransport(self.handler), **kwargs)

    def reference(self, digest=None) -> OciReference:
        return OciReference.parse(f"{HOST}/lab/tools@{digest or self.index_digest}")


def _pull(registry, tmp_path, **kwargs):
    defaults = dict(platform="linux/amd64", layout=tmp_path / "layout", max_bytes=10_000_000)
    return registry.client().pull(registry.reference(), **{**defaults, **kwargs})


# --- References ---


@pytest.mark.parametrize("text", [
    "registry.example/lab/tools:1.0",
    "lab/tools@sha256:" + "a" * 64,
    "registry.example/Lab/tools@sha256:" + "a" * 64,
    "registry.example/lab/tools@sha256:" + "a" * 63,
])
def test_only_host_name_and_digest_references_are_accepted(text):
    with pytest.raises(OciError) as raised:
        OciReference.parse(text)
    assert raised.value.code == "REFERENCE_NOT_PINNED"


def test_docker_hub_names_are_served_by_its_registry_host():
    reference = OciReference.parse("docker.io/library/python@sha256:" + "a" * 64)
    assert reference.endpoint == "https://registry-1.docker.io" and reference.host == "docker.io"


# --- Pulling ---


def test_an_index_resolves_to_the_one_platform_manifest_and_every_byte_is_verified(tmp_path):
    registry = FakeRegistry(platforms=(("linux", "amd64"), ("linux", "arm64")))

    image = _pull(registry, tmp_path)

    assert image.source.kind is SourceKind.OCI_MANIFEST
    assert image.source.digest == registry.platform_manifest and image.source.platform == "linux/amd64"
    index = json.loads((image.layout / "index.json").read_text())
    assert [entry["digest"] for entry in index["manifests"]] == [registry.platform_manifest]
    for name in (image.layout / "blobs" / "sha256").iterdir():
        assert "sha256:" + hashlib.sha256(name.read_bytes()).hexdigest() == "sha256:" + name.name
    assert json.loads((image.layout / "oci-layout").read_text()) == {"imageLayoutVersion": "1.0.0"}


@pytest.mark.parametrize("platforms, code", [
    ((("linux", "arm64"),), "PLATFORM_NOT_AVAILABLE"),
    ((("linux", "amd64"), ("linux", "amd64")), "PLATFORM_AMBIGUOUS"),
])
def test_the_platform_is_never_guessed(tmp_path, platforms, code):
    registry = FakeRegistry(platforms=platforms)
    with pytest.raises(OciError) as raised:
        _pull(registry, tmp_path)
    assert raised.value.code == code


def test_an_image_whose_config_names_another_architecture_is_refused(tmp_path):
    with pytest.raises(OciError) as raised:
        _pull(FakeRegistry(config_arch="arm64"), tmp_path)
    assert raised.value.code == "ARCHITECTURE_MISMATCH"


def test_a_tampered_manifest_or_blob_is_refused(tmp_path):
    registry = FakeRegistry()
    body, media_type = registry.manifests[registry.platform_manifest]
    registry.manifests[registry.platform_manifest] = (body + b" ", media_type)
    with pytest.raises(OciError) as raised:
        _pull(registry, tmp_path)
    assert raised.value.code == "DIGEST_MISMATCH"

    registry = FakeRegistry()
    digest = next(iter(registry.blobs))
    registry.blobs[digest] = b"X" * len(registry.blobs[digest])
    (tmp_path / "second").mkdir()
    with pytest.raises(OciError) as raised:
        _pull(registry, tmp_path / "second")
    assert raised.value.code == "DIGEST_MISMATCH"


def test_an_oversized_image_is_refused_before_any_blob_is_downloaded(tmp_path):
    registry = FakeRegistry()
    with pytest.raises(OciError) as raised:
        _pull(registry, tmp_path, max_bytes=10)
    assert raised.value.code == "IMAGE_TOO_LARGE"
    assert not any("/blobs/" in request.url.path for request in registry.requests)


def test_bearer_authentication_uses_the_member_credential_and_never_reports_it(tmp_path):
    registry = FakeRegistry(auth=True)
    credential = RegistryCredential("member", "PRIVATE-SECRET")

    image = registry.client(credentials={HOST: credential}).pull(
        registry.reference(), platform="linux/amd64", layout=tmp_path / "layout", max_bytes=10_000_000)

    assert image.source.digest == registry.platform_manifest
    token = registry.token_requests[0]
    assert token.url.params["scope"] == "repository:lab/tools:pull"
    assert token.headers["authorization"].startswith("Basic ")


def test_an_anonymous_pull_from_a_private_registry_reports_a_fixed_code(tmp_path):
    registry = FakeRegistry(auth=True)

    def refuse_token(request):
        if request.url.host == "auth.example":
            return httpx.Response(401)
        return registry.handler(request)

    client = OciRegistryClient(transport=httpx.MockTransport(refuse_token))
    with pytest.raises(OciError) as raised:
        client.pull(registry.reference(), platform="linux/amd64", layout=tmp_path / "l", max_bytes=10**7)
    assert raised.value.code == "REGISTRY_UNAUTHORIZED"


def test_a_redirect_to_plain_http_is_refused(tmp_path):
    registry = FakeRegistry()

    def downgrade(request):
        if "/blobs/" in request.url.path and request.url.scheme == "https":
            return httpx.Response(307, headers={"location": "http://storage.example" + request.url.path})
        return registry.handler(request)

    client = OciRegistryClient(transport=httpx.MockTransport(downgrade))
    with pytest.raises(OciError) as raised:
        client.pull(registry.reference(), platform="linux/amd64", layout=tmp_path / "l", max_bytes=10**7)
    assert raised.value.code == "REGISTRY_NOT_HTTPS"


def test_an_unknown_digest_is_reported_as_not_found(tmp_path):
    registry = FakeRegistry()
    with pytest.raises(OciError) as raised:
        registry.client().pull(registry.reference("sha256:" + "f" * 64), platform="linux/amd64",
                               layout=tmp_path / "l", max_bytes=10**7)
    assert raised.value.code == "IMAGE_NOT_FOUND"


# --- Archives ---


def test_an_archive_of_the_same_image_has_the_same_source_identity(tmp_path):
    pulled = _pull(FakeRegistry(), tmp_path)
    archive = tmp_path / "image.tar"
    layout_archive(pulled.layout, archive)

    unpacked = read_archive(archive, platform="linux/amd64", layout=tmp_path / "from-archive", max_bytes=10**7)

    assert unpacked.source == pulled.source  # Same manifest, same environment (DECIDED-C15-D).
    assert not (tmp_path / "from-archive.unpack").exists()


def _archive_with(tmp_path, name, *, link=False) -> Path:
    archive = tmp_path / "bad.tar"
    with tarfile.open(archive, "w:") as tar:
        info = tarfile.TarInfo(name)
        if link:
            info.type, info.linkname = tarfile.SYMTYPE, "/etc/passwd"
            tar.addfile(info)
        else:
            info.size = 1
            tar.addfile(info, io.BytesIO(b"x"))
    return archive


@pytest.mark.parametrize("name, link", [("../escape", False), ("/abs", False), ("blobs/sha256/x", True),
                                        ("unexpected.txt", False), ("blobs/../../escape", False)])
def test_an_archive_cannot_escape_or_smuggle_entries(tmp_path, name, link):
    with pytest.raises(OciError) as raised:
        read_archive(_archive_with(tmp_path, name, link=link), platform="linux/amd64",
                     layout=tmp_path / "layout", max_bytes=10**7)
    assert raised.value.code == "ARCHIVE_INVALID"
    assert not (tmp_path / "layout").exists() and not (tmp_path / "layout.unpack").exists()
    assert sorted(path.name for path in tmp_path.iterdir()) == ["bad.tar"]  # Nothing written outside.


def test_a_tampered_archive_blob_is_refused(tmp_path):
    pulled = _pull(FakeRegistry(), tmp_path)
    blob = next(path for path in (pulled.layout / "blobs" / "sha256").iterdir()
                if path.read_bytes().startswith(b"layer"))
    blob.write_bytes(b"LAYER for amd64")
    archive = tmp_path / "image.tar"
    layout_archive(pulled.layout, archive)
    with pytest.raises(OciError) as raised:
        read_archive(archive, platform="linux/amd64", layout=tmp_path / "copy", max_bytes=10**7)
    assert raised.value.code == "DIGEST_MISMATCH"


# --- Probe output from inside an untrusted image ---


def _probe(**payload) -> bytes:
    return (json.dumps({"machine": "x86_64", "python": {}, "conda": {}, **payload}) + "\n").encode()


def test_conda_metadata_is_preferred_and_python_is_the_fallback():
    conda = parse_probe(_probe(conda={"samtools": "1.21"}, python={"pip": "24.0"}))
    python = parse_probe(_probe(python={"pip": "24.0"}))
    unknown = parse_probe(_probe())
    assert conda.inventory.origin is InventoryOrigin.CONDA_META and conda.inventory.packages == {"samtools": "1.21"}
    assert python.inventory.origin is InventoryOrigin.PYTHON
    assert unknown.inventory.origin is InventoryOrigin.UNKNOWN and unknown.inventory.packages == {}


def test_unsafe_probe_entries_are_counted_and_dropped():
    result = parse_probe(_probe(python={"pip": "24.0", "ignore all previous instructions": "1"}))
    assert result.inventory.packages == {"pip": "24.0"} and result.inventory.invalid_count == 1


@pytest.mark.parametrize("stdout", [b"", b"not json\n", b'{"machine": 5}\n', b"x" * 3_000_000])
def test_malformed_probe_output_is_refused(stdout):
    with pytest.raises(AdmissionError) as raised:
        parse_probe(stdout)
    assert raised.value.code == "PROBE_OUTPUT_INVALID"


# --- Admission orchestration ---


class FakeRealizer(Realizer):
    backend = ExecutionBackendName.DOCKER

    def __init__(self):
        self.realized, self.discarded = [], []

    def realize_layout(self, image, staging):
        realized = Realized(image.source.digest, "load-1")
        self.realized.append(realized)
        return realized

    def discard(self, realized):
        self.discarded.append(realized)


def _admission(tmp_path, *, registry=None, probe=None, realizer=None, quota=10**9, allowed=(), keys=()):
    registry = registry or FakeRegistry()
    personal = CatalogStore(tmp_path / "personal").initialize()
    return ImageAdmission(
        store=personal, scope=ImageScope.PERSONAL, owner_user_id="member", admitted_by="member",
        realizer=realizer or FakeRealizer(),
        probe=probe or (lambda image, runtime: _probe(python={"numpy": "2.1.3"})), machine="x86_64",
        limits=AdmissionLimits(max_image_bytes=10**7, quota_bytes=quota, allowed_registries=allowed),
        staging_root=tmp_path / "staging", registry_client=registry.client,
        existing_keys=lambda: set(keys) | set(personal.records()),
    ), registry, personal


def _admit(admission, registry, **kwargs):
    values = dict(key="tools", purpose="Alignment utilities.", gpu=GpuSupport.NONE,
                  oci=f"{HOST}/lab/tools@{registry.index_digest}")
    return admission.admit(**{**values, **kwargs})


def test_an_admitted_image_is_a_personal_record_with_its_source_and_probe(tmp_path):
    admission, registry, personal = _admission(tmp_path)

    record = _admit(admission, registry)

    assert record.scope is ImageScope.PERSONAL and record.owner_user_id == "member"
    assert record.source.digest == registry.platform_manifest
    assert record.realizations[0].resolved_image == registry.platform_manifest
    assert record.realizations[0].produced_by.startswith("oci:")
    assert record.inventory.packages == {"numpy": "2.1.3"} and record.purpose == "Alignment utilities."
    assert personal.records() == {"tools": record}
    assert list((tmp_path / "staging").iterdir()) == []


def test_the_quota_is_checked_before_anything_is_realized(tmp_path):
    realizer = FakeRealizer()
    admission, registry, personal = _admission(tmp_path, realizer=realizer, quota=10)
    with pytest.raises(AdmissionError) as raised:
        _admit(admission, registry)
    assert raised.value.code == "MEMBER_QUOTA_EXCEEDED"
    assert realizer.realized == [] and personal.records() == {}


@pytest.mark.parametrize("probe, code", [
    (lambda image, runtime: _probe(machine="aarch64"), "ARCHITECTURE_MISMATCH"),
    (lambda image, runtime: b"garbage", "PROBE_OUTPUT_INVALID"),
])
def test_a_failed_probe_discards_the_realization_and_records_nothing(tmp_path, probe, code):
    realizer = FakeRealizer()
    admission, registry, personal = _admission(tmp_path, realizer=realizer, probe=probe)
    with pytest.raises(AdmissionError) as raised:
        _admit(admission, registry)
    assert raised.value.code == code
    assert realizer.discarded == realizer.realized and personal.records() == {}
    assert list((tmp_path / "staging").iterdir()) == []


@pytest.mark.parametrize("kwargs, setup, code", [
    ({}, {"keys": ("tools",)}, "IMAGE_KEY_TAKEN"),
    ({"purpose": "Reads /home/member/data"}, {}, "PURPOSE_INVALID"),
    ({}, {"allowed": ("quay.io",)}, "REGISTRY_NOT_ALLOWED"),
    ({"oci": None, "sif": Path("/dev/null")}, {}, "SIF_INVALID"),
    ({"oci": None}, {}, "FORM_REQUIRED"),
])
def test_requests_that_cannot_be_admitted_have_no_effect(tmp_path, kwargs, setup, code):
    admission, registry, personal = _admission(tmp_path, **setup)
    with pytest.raises(AdmissionError) as raised:
        _admit(admission, registry, **kwargs)
    assert raised.value.code == code and personal.records() == {}


def test_a_sif_is_its_own_source_on_apptainer(tmp_path):
    sif = tmp_path / "tools.sif"
    sif.write_bytes(b"SIF image bytes")
    realizer = ApptainerRealizer(sif_store=SifStore(tmp_path / "sif"), build=lambda *_: None)
    admission, registry, _ = _admission(tmp_path, realizer=realizer)

    record = _admit(admission, registry, oci=None, sif=sif)

    assert record.source.kind is SourceKind.SIF and record.source.digest == _digest(b"SIF image bytes")
    assert record.realizations[0].backend is ExecutionBackendName.APPTAINER


def test_a_sif_cannot_be_admitted_on_docker(tmp_path):
    sif = tmp_path / "tools.sif"
    sif.write_bytes(b"SIF image bytes")
    admission, registry, _ = _admission(tmp_path)
    with pytest.raises(AdmissionError) as raised:
        _admit(admission, registry, oci=None, sif=sif)
    assert raised.value.code == "FORM_UNSUPPORTED_ON_BACKEND"


# --- Credentials and SIF stores ---


def test_registry_credentials_must_be_private_to_the_member(tmp_path):
    path = tmp_path / "registries.json"
    path.write_text(json.dumps({"hosts": {HOST: {"username": "member", "secret": "s"}}}))
    path.chmod(0o644)
    with pytest.raises(AdmissionError) as raised:
        load_registry_credentials(path)
    assert raised.value.code == "REGISTRY_CREDENTIALS_PERMISSIONS"
    path.chmod(0o600)
    assert load_registry_credentials(path) == {HOST: RegistryCredential("member", "s")}
    assert load_registry_credentials(tmp_path / "absent.json") == {}


def test_a_sif_chain_never_falls_through_a_tampered_file(tmp_path):
    first, second = SifStore(tmp_path / "configured"), SifStore(tmp_path / "personal")
    sif = tmp_path / "a.sif"
    sif.write_bytes(b"one image")
    reference = second.register(sif)
    chain = SifStoreChain((first, second))
    assert chain.verify(reference) == second.path_for(reference)

    first.register(sif)
    stored = first.path_for(reference)
    stored.chmod(0o644)
    stored.write_bytes(b"tampered")
    stored.chmod(0o444)
    with pytest.raises(SifStoreError):
        chain.verify(reference)


# --- Lab policy ---


def test_the_lab_policy_is_set_by_an_administrator_and_read_by_members(tmp_path, monkeypatch):
    from labbioagentos import local_lab
    from labbioagentos.local_lab import LabArea, LabError

    area = LabArea.initialize(tmp_path / "lab", lab_id="lab-one", admin_user_id="admin", group=None)
    assert area.policy().max_image_bytes == 16 * 1_073_741_824 and area.policy().allowed_registries == ()

    area.set_policy(reason="Only the lab registry.", allowed_registries=("registry.example",),
                    member_quota_bytes=2 * 1_073_741_824)

    policy = LabArea(tmp_path / "lab").policy()
    assert policy.allowed_registries == ("registry.example",) and policy.actor == "admin"
    assert policy.member_quota_bytes == 2 * 1_073_741_824 and policy.max_image_bytes == 16 * 1_073_741_824
    with pytest.raises(LabError):
        area.set_policy(reason="Bad host.", allowed_registries=("https://registry.example",))
    monkeypatch.setattr(local_lab.os, "getuid", lambda: 99999)
    with pytest.raises(LabError):
        area.set_policy(reason="Not the holder.", lab_member_quota_bytes=1)


def test_admission_requires_a_lab_and_keeps_env_keys_for_builds(tmp_path, capsys):
    from labbioagentos import cli
    from test_c15_image_identity import _write

    path = _write(tmp_path)
    arguments = ["image-admit", "--config", str(path), "--key", "tools", "--purpose", "Tools.",
                 "--oci", f"{HOST}/lab/tools@sha256:" + "a" * 64]
    assert cli.main(arguments) == 1
    assert json.loads(capsys.readouterr().out.strip())["error"] == "ADMISSION_REQUIRES_LAB"


# --- C15 step 9b: what a failed Apptainer admission stored is removed ---


def test_a_failed_apptainer_admission_removes_the_sif_it_stored(tmp_path):
    from labbioagentos.execution.models import GpuSupport

    store = SifStore(tmp_path / "sif")
    realizer = ApptainerRealizer(sif_store=store, build=lambda *_: None)
    admission, registry, personal = _admission(tmp_path, realizer=realizer)
    sif = tmp_path / "tools.sif"
    sif.write_bytes(b"SIF image bytes")

    with pytest.raises(AdmissionError) as raised:  # The probe finds no CUDA runtime.
        _admit(admission, registry, oci=None, sif=sif, gpu=GpuSupport.SUPPORTED)

    assert raised.value.code == "CUDA_RUNTIME_MISSING" and personal.records() == {}
    assert list((tmp_path / "sif").iterdir()) == []


def test_a_failed_apptainer_admission_keeps_a_sif_that_was_already_stored(tmp_path):
    from labbioagentos.execution.models import GpuSupport

    store = SifStore(tmp_path / "sif")
    sif = tmp_path / "tools.sif"
    sif.write_bytes(b"SIF image bytes")
    reference = store.register(sif)  # Another record already uses this file.
    admission, registry, _personal = _admission(
        tmp_path, realizer=ApptainerRealizer(sif_store=store, build=lambda *_: None))

    with pytest.raises(AdmissionError):
        _admit(admission, registry, oci=None, sif=sif, gpu=GpuSupport.SUPPORTED)

    assert store.verify(reference)


def test_a_store_that_cannot_register_is_a_realization_failure(tmp_path, monkeypatch):
    from labbioagentos.execution.apptainer import SifStoreError

    store = SifStore(tmp_path / "sif")

    def broken(source):
        raise SifStoreError("SIF store is unavailable")

    monkeypatch.setattr(store, "register_new", broken)
    admission, registry, _personal = _admission(
        tmp_path, realizer=ApptainerRealizer(sif_store=store, build=lambda *_: None))
    sif = tmp_path / "tools.sif"
    sif.write_bytes(b"SIF image bytes")
    with pytest.raises(AdmissionError) as raised:
        _admit(admission, registry, oci=None, sif=sif)
    assert raised.value.code == "REALIZATION_FAILED"
