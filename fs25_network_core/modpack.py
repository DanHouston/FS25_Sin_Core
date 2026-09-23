"""Deterministic, filesystem-only FS25 modpack capture and publication.

The publisher deliberately has no knowledge of Google Drive APIs.  A mounted
or synchronized filesystem is just another publication root.  A modpack is
created only by an explicit approval operation with an explicit list of ZIP
files; merely adding a ZIP to a development directory never publishes it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile, ZipInfo


class ModpackError(ValueError):
    """A safe, actionable modpack validation or publication failure."""


_SAFE_SERVER_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_SAFE_FILENAME = re.compile(r"^[^\\/:*?\"<>|\x00-\x1f]+\.zip$", re.IGNORECASE)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_server_key(value: str) -> str:
    value = str(value or "")
    if not _SAFE_SERVER_KEY.fullmatch(value):
        raise ModpackError("server_key must contain 1-64 letters, numbers, '-' or '_'")
    return value


def _safe_filename(value: str) -> str:
    value = str(value or "")
    if not _SAFE_FILENAME.fullmatch(value) or Path(value).name != value:
        raise ModpackError(f"invalid mod ZIP filename: {value!r}")
    return value


def _safe_display_name(value: str) -> str:
    value = " ".join(str(value or "").split()).strip()
    if not value:
        raise ModpackError("server display_name is required")
    # The friendly name is used as a filename, never as a path.
    value = re.sub(r"[^A-Za-z0-9._ -]+", "_", value).strip(" .")
    if not value:
        raise ModpackError("server display_name has no safe filename characters")
    return value


def _write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n", encoding="utf-8")


def _deterministic_zip(destination: Path, members: list[tuple[str, bytes]]) -> None:
    """Write a reproducible ZIP with stable metadata and member ordering."""
    with ZipFile(destination, "w", ZIP_DEFLATED) as archive:
        for name, content in sorted(members, key=lambda item: item[0]):
            info = ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, content)


@dataclass(frozen=True)
class ModpackPaths:
    """Operator-provided filesystem paths; no production path is implicit."""

    source_dir: Path
    publication_root: Path
    client_mod_dir: Path | None = None

    @classmethod
    def from_environment(cls, environ=None) -> "ModpackPaths":
        environ = os.environ if environ is None else environ

        def required(name):
            value = str(environ.get(name, "")).strip()
            if not value:
                raise ModpackError(f"Missing {name}; pass an explicit modpack path")
            return Path(value).expanduser()

        client = str(environ.get("SIN_MODPACK_CLIENT_MOD_DIR", "")).strip()
        return cls(required("SIN_MODPACK_SOURCE_DIR"), required("SIN_MODPACK_PUBLICATION_ROOT"),
                   Path(client).expanduser() if client else None)


@dataclass(frozen=True)
class ServerIdentity:
    """The only server identity accepted by the publisher.

    Production callers should construct this from a ``sin_servers`` record
    returned by ``ServerRegistry.info``; the publisher does not maintain a
    second server roster.
    """

    server_key: str
    display_name: str

    @classmethod
    def from_registry_record(cls, record: dict) -> "ServerIdentity":
        if not isinstance(record, dict):
            raise ModpackError("server registry record is required")
        return cls(_safe_server_key(record.get("server_key")), _safe_display_name(record.get("display_name")))


@dataclass(frozen=True)
class ApprovedModpack:
    server_key: str
    display_name: str
    modpack_version: str
    release_dir: Path
    manifest: dict


class ModpackManager:
    """Capture approved mod ZIPs, validate releases, publish, and synchronize."""

    def __init__(self, paths: ModpackPaths):
        self.paths = paths

    def _server_root(self, identity: ServerIdentity) -> Path:
        return self.paths.publication_root / identity.server_key

    def _ensure_publication_root(self) -> Path:
        root = self.paths.publication_root.expanduser()
        source = self.paths.source_dir.expanduser().resolve()
        resolved_root = root.resolve()
        try:
            resolved_root.relative_to(source)
        except ValueError:
            pass
        else:
            raise ModpackError("publication root must not be the source directory or inside it")
        if root.exists() and not root.is_dir():
            raise ModpackError(f"publication root is not a directory: {root}")
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise ModpackError(f"publication root is not writable: {root}") from error
        return root

    @staticmethod
    def _version(value: str) -> str:
        value = str(value or "").strip()
        if (not value or Path(value).name != value or "\\" in value or "/" in value
                or value in {".", ".."} or any(ord(char) < 32 for char in value)):
            raise ModpackError("modpack_version must be a non-empty directory name")
        return value

    def _manifest(self, identity: ServerIdentity, version: str, mods: list[dict], filename: str) -> dict:
        return {
            "manifest_schema": "sin.fs25-modpack/1",
            "server_key": identity.server_key,
            "server_display_name": identity.display_name,
            "modpack_version": version,
            "modpack_filename": filename,
            "mods": mods,
        }

    @staticmethod
    def _validate_zip(path: Path) -> None:
        try:
            with ZipFile(path) as archive:
                if archive.testzip() is not None:
                    raise ModpackError(f"mod ZIP failed CRC validation: {path.name}")
        except BadZipFile as error:
            raise ModpackError(f"mod ZIP is not a valid ZIP archive: {path.name}") from error

    def _selected_sources(self, selected_mods) -> list[tuple[str, Path]]:
        if not selected_mods:
            raise ModpackError("explicit approved mod filenames are required; refusing to capture every ZIP")
        names = [_safe_filename(name) for name in selected_mods]
        if len(set(name.casefold() for name in names)) != len(names):
            raise ModpackError("approved mod filenames must be unique")
        source_root = self.paths.source_dir.resolve()
        if not source_root.is_dir():
            raise ModpackError(f"mod source directory does not exist: {source_root}")
        result = []
        for name in sorted(names, key=str.casefold):
            source = (source_root / name).resolve()
            if source.parent != source_root or not source.is_file():
                raise ModpackError(f"approved mod source is missing: {name}")
            self._validate_zip(source)
            result.append((name, source))
        return result

    def capture_approved(self, server_record: dict, modpack_version: str, selected_mods) -> ApprovedModpack:
        """Capture an explicit tested/approved modset into ``Releases``.

        This operation never changes ``Current``.  A later explicit
        :meth:`publish_approved` call is required to make the release live.
        """
        identity = ServerIdentity.from_registry_record(server_record)
        version = self._version(modpack_version)
        sources = self._selected_sources(selected_mods)
        self._ensure_publication_root()
        friendly = _safe_display_name(identity.display_name)
        filename = f"{friendly}-Modpack.zip"
        server_root = self._server_root(identity)
        releases = server_root / "Releases"
        release = releases / version
        releases.mkdir(parents=True, exist_ok=True)
        temp_release = releases / f".{version}.capture-{uuid.uuid4().hex}"
        try:
            temp_release.mkdir(parents=True)
            mods_dir = temp_release / "mods"
            mods_dir.mkdir()
            mods = []
            zip_members = []
            for name, source in sources:
                try:
                    raw = source.read_bytes()
                except OSError as error:
                    raise ModpackError(f"approved mod source cannot be read: {name}") from error
                target = mods_dir / name
                target.write_bytes(raw)
                digest = hashlib.sha256(raw).hexdigest()
                mods.append({"filename": name, "size": len(raw), "sha256": digest})
                zip_members.append((f"mods/{name}", raw))
            manifest = self._manifest(identity, version, mods, filename)
            _write_json(temp_release / "manifest.json", manifest)
            _deterministic_zip(temp_release / filename, zip_members)
            self._validate_release_dir(temp_release, identity=identity, version=version)
            if release.exists():
                existing = self._read_valid_release(release, identity, version)
                if existing.manifest != manifest:
                    raise ModpackError(f"release already exists with different approved modset: {release}")
                shutil.rmtree(temp_release)
                return existing
            temp_release.rename(release)
            return ApprovedModpack(identity.server_key, identity.display_name, version, release, manifest)
        except Exception:
            if temp_release.exists():
                shutil.rmtree(temp_release, ignore_errors=True)
            raise

    def _read_valid_release(self, release: Path, identity: ServerIdentity, version: str) -> ApprovedModpack:
        self._validate_release_dir(release, identity=identity, version=version)
        manifest = json.loads((release / "manifest.json").read_text(encoding="utf-8"))
        return ApprovedModpack(identity.server_key, identity.display_name, version, release, manifest)

    def _validate_release_dir(self, release: Path, identity: ServerIdentity, version: str) -> dict:
        if release.is_symlink() or not release.is_dir():
            raise ModpackError(f"approved release does not exist: {release}")
        manifest_path = release / "manifest.json"
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise ModpackError(f"release is missing manifest.json: {release}")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise ModpackError(f"release manifest is invalid: {release}") from error
        if not isinstance(manifest, dict):
            raise ModpackError("release manifest must be a JSON object")
        expected_keys = {"manifest_schema", "server_key", "server_display_name", "modpack_version", "modpack_filename", "mods"}
        if set(manifest) != expected_keys or manifest.get("manifest_schema") != "sin.fs25-modpack/1":
            raise ModpackError("release manifest schema is invalid")
        if (manifest["server_key"] != identity.server_key
                or manifest["server_display_name"] != identity.display_name
                or manifest["modpack_version"] != version):
            raise ModpackError("release identity does not match the selected server/version")
        filename = _safe_filename(manifest["modpack_filename"])
        if not filename.lower().endswith("-modpack.zip"):
            raise ModpackError("release manifest has an invalid modpack filename")
        pack = release / filename
        mods_dir = release / "mods"
        if pack.is_symlink() or mods_dir.is_symlink() or not pack.is_file() or not mods_dir.is_dir():
            raise ModpackError("release must contain its Modpack.zip and mods directory")
        records = manifest["mods"]
        if not isinstance(records, list) or not records:
            raise ModpackError("release manifest must contain at least one mod")
        seen = set()
        members = []
        for record in records:
            if not isinstance(record, dict) or set(record) != {"filename", "size", "sha256"}:
                raise ModpackError("release manifest contains an invalid mod record")
            name = _safe_filename(record["filename"])
            if name.casefold() in seen:
                raise ModpackError("release manifest contains duplicate mod filenames")
            seen.add(name.casefold())
            path = mods_dir / name
            try:
                expected_size = int(record["size"])
            except (TypeError, ValueError) as error:
                raise ModpackError(f"release manifest has an invalid size: {name}") from error
            if expected_size < 0 or not re.fullmatch(r"[0-9a-fA-F]{64}", str(record["sha256"] or "")):
                raise ModpackError(f"release manifest has invalid hash metadata: {name}")
            if path.is_symlink() or not path.is_file() or path.stat().st_size != expected_size:
                raise ModpackError(f"release mod does not match manifest: {name}")
            if _sha256(path) != str(record["sha256"]).lower():
                raise ModpackError(f"release mod hash does not match manifest: {name}")
            self._validate_zip(path)
            members.append((f"mods/{name}", path.read_bytes()))
        expected_names = {Path(name).name for name, _ in members}
        actual_entries = list(mods_dir.iterdir())
        actual_names = {path.name for path in actual_entries if path.is_file() and not path.is_symlink()}
        if any(not path.is_file() or path.is_symlink() for path in actual_entries) or actual_names != expected_names:
            raise ModpackError("release mods directory contains unlisted files")
        try:
            with ZipFile(pack) as archive:
                archive_names = archive.namelist()
                if (archive.testzip() is not None or len(archive_names) != len(set(archive_names))
                        or set(archive_names) != {name for name, _ in members}):
                    raise ModpackError("combined Modpack.zip is incomplete or corrupt")
                for name, raw in members:
                    if archive.read(name) != raw:
                        raise ModpackError(f"combined Modpack.zip member differs from {name}")
        except BadZipFile as error:
            raise ModpackError("combined Modpack.zip is not a valid ZIP archive") from error
        return manifest

    def publish_approved(self, server_record: dict, modpack_version: str) -> ApprovedModpack:
        """Validate a captured release, then replace ``Current`` as a unit."""
        identity = ServerIdentity.from_registry_record(server_record)
        version = self._version(modpack_version)
        release = self._server_root(identity) / "Releases" / version
        approved = self._read_valid_release(release, identity, version)
        current = self._server_root(identity) / "Current"
        if current.exists():
            existing = self._read_current(identity)
            if existing.manifest == approved.manifest:
                return existing
        stage = current.parent / f".Current.publish-{uuid.uuid4().hex}"
        backup = current.parent / f".Current.previous-{uuid.uuid4().hex}"
        try:
            try:
                shutil.copytree(release, stage)
            except OSError as error:
                raise ModpackError(f"publication staging failed: {stage}") from error
            self._validate_release_dir(stage, identity, version)
            if current.exists():
                current.rename(backup)
            try:
                stage.rename(current)
            except Exception:
                if backup.exists() and not current.exists():
                    backup.rename(current)
                raise
            if backup.exists():
                shutil.rmtree(backup)
            return self._read_current(identity)
        except Exception:
            if stage.exists():
                shutil.rmtree(stage, ignore_errors=True)
            if backup.exists() and not current.exists():
                backup.rename(current)
            raise

    def _read_current(self, identity: ServerIdentity) -> ApprovedModpack:
        current = self._server_root(identity) / "Current"
        if not current.is_dir():
            raise ModpackError("Current modpack is not published")
        try:
            manifest = json.loads((current / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise ModpackError("Current modpack manifest is invalid") from error
        if not isinstance(manifest, dict):
            raise ModpackError("Current modpack manifest must be a JSON object")
        version = self._version(manifest.get("modpack_version"))
        return ApprovedModpack(identity.server_key, identity.display_name, version, current,
                               self._validate_release_dir(current, identity, version))

    def validate_current(self, server_record: dict) -> ApprovedModpack:
        return self._read_current(ServerIdentity.from_registry_record(server_record))

    def sync_client(self, server_record: dict, client_dir: Path | None = None, strict: bool = False) -> dict:
        """Synchronize managed ZIPs; unmanaged local ZIPs survive by default."""
        identity = ServerIdentity.from_registry_record(server_record)
        current = self._read_current(identity)
        if client_dir is None and self.paths.client_mod_dir is None:
            raise ModpackError("client mod directory is required for synchronization")
        target = Path(client_dir or self.paths.client_mod_dir).expanduser()
        try:
            target.resolve().relative_to(self.paths.publication_root.expanduser().resolve())
        except ValueError:
            pass
        else:
            raise ModpackError("client mod directory must not be inside the publication root")
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise ModpackError(f"client mod directory is not writable: {target}") from error
        expected = {record["filename"]: record for record in current.manifest["mods"]}
        for name, record in expected.items():
            source = current.release_dir / "mods" / name
            destination = target / name
            temporary = target / f".{name}.sync-{uuid.uuid4().hex}"
            try:
                shutil.copyfile(source, temporary)
                if temporary.stat().st_size != int(record["size"]) or _sha256(temporary) != record["sha256"]:
                    raise ModpackError(f"client sync validation failed: {name}")
                os.replace(temporary, destination)
            except ModpackError:
                temporary.unlink(missing_ok=True)
                raise
            except OSError as error:
                temporary.unlink(missing_ok=True)
                raise ModpackError(f"client sync failed for {name}") from error
        removed = []
        if strict:
            for path in target.glob("*.zip"):
                if path.name not in expected:
                    path.unlink()
                    removed.append(path.name)
        return {"server_key": identity.server_key, "modpack_version": current.modpack_version,
                "synchronized": sorted(expected), "removed": sorted(removed), "strict": bool(strict)}


def _cli() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("capture", "publish", "sync", "validate"))
    parser.add_argument("--server-key", required=True)
    parser.add_argument("--server-name", required=True)
    parser.add_argument("--version")
    parser.add_argument("--mod", action="append", dest="mods")
    parser.add_argument("--source-dir")
    parser.add_argument("--publication-root")
    parser.add_argument("--client-dir")
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()
    try:
        source = Path(args.source_dir).expanduser() if args.source_dir else None
        publication = Path(args.publication_root).expanduser() if args.publication_root else None
        client = Path(args.client_dir).expanduser() if args.client_dir else None
        if source is None:
            source = Path(os.environ.get("SIN_MODPACK_SOURCE_DIR", "")).expanduser()
        if publication is None:
            publication = Path(os.environ.get("SIN_MODPACK_PUBLICATION_ROOT", "")).expanduser()
        if client is None and os.environ.get("SIN_MODPACK_CLIENT_MOD_DIR", "").strip():
            client = Path(os.environ["SIN_MODPACK_CLIENT_MOD_DIR"]).expanduser()
        if not str(source) or str(source) == ".":
            raise ModpackError("Missing SIN_MODPACK_SOURCE_DIR or --source-dir")
        if not str(publication) or str(publication) == ".":
            raise ModpackError("Missing SIN_MODPACK_PUBLICATION_ROOT or --publication-root")
        paths = ModpackPaths(source, publication, client)
        manager = ModpackManager(paths)
        record = {"server_key": args.server_key, "display_name": args.server_name}
        if args.operation == "capture":
            if not args.version:
                raise ModpackError("--version is required for capture")
            result = manager.capture_approved(record, args.version, args.mods)
        elif args.operation == "publish":
            if not args.version:
                raise ModpackError("--version is required for publish")
            result = manager.publish_approved(record, args.version)
        elif args.operation == "validate":
            result = manager.validate_current(record)
        else:
            result = manager.sync_client(record, strict=args.strict)
        if isinstance(result, ApprovedModpack):
            print(json.dumps(result.manifest, indent=2, sort_keys=True))
        else:
            print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except ModpackError as error:
        parser.exit(2, f"modpack error: {error}\n")


if __name__ == "__main__":
    raise SystemExit(_cli())
