"""Shared bootstrap: ensure platform packages exist (local or git clone)."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None


ROOT_MARKERS = ("platform.manifest.yaml", ".git")


def find_workspace_root(start: Path | None = None) -> Path | None:
    """Walk up looking for platform.manifest.yaml (monorepo root)."""
    cur = (start or Path.cwd()).resolve()
    for _ in range(8):
        if (cur / "platform.manifest.yaml").exists():
            return cur
        if cur.parent == cur:
            break
        cur = cur.parent
    return None


def find_manifest_file(start: Path | None = None) -> Path | None:
    """Locate platform.manifest.yaml (repo root or vendored under package/scripts/)."""
    here = Path(__file__).resolve()
    candidates = [
        find_workspace_root(start),
        here.parent / "platform.manifest.yaml",
        here.parents[1] / "platform.manifest.yaml",
        Path.cwd() / "platform.manifest.yaml",
        Path.cwd().parent / "platform.manifest.yaml",
    ]
    for c in candidates:
        if c is None:
            continue
        path = c if c.suffix == ".yaml" else Path(c) / "platform.manifest.yaml"
        if path.exists():
            return path.resolve()
    return None


def load_manifest(root: Path | None = None) -> dict[str, Any]:
    if yaml is None:
        raise RuntimeError("PyYAML required. Run: pip install pyyaml")
    if root and (Path(root) / "platform.manifest.yaml").exists():
        manifest_path = Path(root) / "platform.manifest.yaml"
    else:
        manifest_path = find_manifest_file(root)
    if manifest_path is None:
        raise FileNotFoundError(
            "platform.manifest.yaml not found. Clone "
            "https://github.com/kashewknutt/social-agent-platform first."
        )
    data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    # Workspace root: monorepo root if manifest is there; else parent of package
    if manifest_path.parent.name == "scripts":
        package_dir = manifest_path.parents[1]
        workspace = package_dir.parent
        if (package_dir / "platform.manifest.yaml").exists():
            workspace = package_dir
        if (workspace / "platform.manifest.yaml").exists():
            pass
        elif (package_dir.parent / "platform.manifest.yaml").exists():
            workspace = package_dir.parent
    else:
        workspace = manifest_path.parent
    data["_root"] = workspace
    data["_manifest_path"] = manifest_path
    return data


def run(cmd: list[str], *, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    print(f"+ {' '.join(cmd)}")
    return subprocess.run(cmd, cwd=str(cwd) if cwd else None, check=check)


def package_dir(workspace: Path, name: str, manifest: dict[str, Any]) -> Path:
    rel = manifest["packages"][name]["path"]
    return (workspace / rel).resolve()


def ensure_package(
    name: str,
    *,
    workspace: Path,
    manifest: dict[str, Any],
) -> Path:
    """Ensure package exists under workspace; clone plugins if missing."""
    dest = package_dir(workspace, name, manifest)
    if dest.exists() and any(p for p in dest.iterdir() if p.name != ".gitkeep"):
        print(f"[ok] {name} already present at {dest}")
        return dest

    meta = manifest["packages"][name]
    kind = meta.get("kind", "core")
    branch = meta.get("branch") or manifest.get("platform_branch", "main")
    dest.parent.mkdir(parents=True, exist_ok=True)

    # External package (library or plugin) — clone whole repo into path
    if kind in ("plugin", "library") or meta.get("repo"):
        repo = meta.get("repo")
        if not repo:
            raise RuntimeError(f"Package {name} is missing repo URL in platform.manifest.yaml")
        label = "plugin" if kind == "plugin" else "package"
        print(f"-> Cloning {label} {name} from {repo} (branch {branch})")
        if dest.exists():
            shutil.rmtree(dest)
        run(["git", "clone", "--branch", branch, "--depth", "1", repo, str(dest)])
        print(f"[ok] {name} ready at {dest}")
        return dest

    # Core package should already ship inside the platform clone
    if dest.exists():
        print(f"[ok] {name} already present at {dest}")
        return dest

    # Fallback: sparse-checkout from platform_repo (recovery / partial checkout)
    repo = manifest["platform_repo"]
    subdir = meta["path"]
    print(f"-> Fetching core package {name} from platform repo ({subdir})")
    with tempfile.TemporaryDirectory(prefix="platform-sparse-") as tmp:
        tmp_path = Path(tmp) / "repo"
        run(
            [
                "git",
                "clone",
                "--filter=blob:none",
                "--sparse",
                "--branch",
                branch,
                "--depth",
                "1",
                repo,
                str(tmp_path),
            ]
        )
        run(["git", "sparse-checkout", "set", subdir], cwd=tmp_path)
        src = tmp_path / subdir
        if not src.exists():
            raise FileNotFoundError(f"{subdir} not found in {repo}@{branch}")
        shutil.copytree(src, dest)
    print(f"[ok] {name} ready at {dest}")
    return dest


def resolve_packages(profile: str | None, packages: list[str] | None, manifest: dict[str, Any]) -> list[str]:
    if packages:
        selected = list(packages)
    elif profile:
        if profile not in manifest["profiles"]:
            known = ", ".join(manifest["profiles"])
            raise SystemExit(f"Unknown profile {profile!r}. Choose one of: {known}")
        selected = list(manifest["profiles"][profile]["packages"])
    else:
        selected = list(manifest["profiles"]["fleet"]["packages"])

    # Expand depends_on
    ordered: list[str] = []
    seen: set[str] = set()

    def add(pkg: str) -> None:
        if pkg in seen:
            return
        for dep in manifest["packages"].get(pkg, {}).get("depends_on", []):
            add(dep)
        seen.add(pkg)
        ordered.append(pkg)

    for p in selected:
        add(p)
    return ordered


def ensure_packages(
    names: list[str],
    *,
    workspace: Path,
    manifest: dict[str, Any],
) -> list[Path]:
    return [ensure_package(n, workspace=workspace, manifest=manifest) for n in names]


def create_venv(workspace: Path) -> Path:
    venv = workspace / ".venv"
    if not (venv / ("Scripts" if os.name == "nt" else "bin") / ("python.exe" if os.name == "nt" else "python")).exists():
        print(f"-> Creating venv at {venv}")
        run([sys.executable, "-m", "venv", str(venv)])
    return venv


def venv_python(venv: Path) -> Path:
    if os.name == "nt":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def pip_install_editable(python: Path, package_paths: list[Path]) -> None:
    for path in package_paths:
        run([str(python), "-m", "pip", "install", "-e", str(path)])


def write_orchestrator_yaml(
    workspace: Path,
    enabled_package_names: list[str],
    manifest: dict[str, Any],
) -> None:
    orch = workspace / "orchestrator"
    if not orch.exists():
        return
    bots_block = []
    for pkg_name, meta in manifest["packages"].items():
        bot_id = meta.get("bot_id")
        if not bot_id:
            continue
        rel = meta["path"]
        # Paths in orchestrator.yaml are relative to the orchestrator/ folder
        orch_rel = Path("..") / rel
        exists = package_dir(workspace, pkg_name, manifest).exists()
        enabled = pkg_name in enabled_package_names and exists
        title = meta.get("description") or bot_id
        # Prefer short name
        short = {
            "instagram": "Instagram",
            "linkedin": "LinkedIn",
            "x": "X",
        }.get(bot_id, bot_id.title())
        bots_block.append(
            f"  - id: {bot_id}\n"
            f"    name: {short}\n"
            f"    path: {orch_rel.as_posix()}\n"
            f"    port: {int(meta.get('port', 7411))}\n"
            f"    enabled: {'true' if enabled else 'false'}\n"
            f"    start_command: {meta.get('start_command', 'python -m app')}"
        )
    content = (
        "host: 127.0.0.1\n"
        "port: 7400\n"
        "bots:\n" + "\n".join(bots_block) + "\n"
    )
    (orch / "orchestrator.yaml").write_text(content, encoding="utf-8")
    print(f"[ok] Wrote {orch / 'orchestrator.yaml'}")


def copy_env_examples(workspace: Path, packages: list[str], manifest: dict[str, Any]) -> None:
    for name in packages:
        example = manifest["packages"][name].get("env_example")
        if not example:
            continue
        src = workspace / example
        dest = src.with_name(".env") if src.name == ".env.example" else src.parent / ".env"
        # env_example paths look like bot-instagram/.env.example
        src = workspace / example
        dest = src.parent / ".env"
        if src.exists() and not dest.exists():
            shutil.copy(src, dest)
            print(f"[ok] Created {dest} - set MOONSHOT_API_KEY before live runs")


def interactive_profile(manifest: dict[str, Any]) -> str:
    profiles = manifest["profiles"]
    print("\nWhat do you want to set up?\n")
    keys = list(profiles.keys())
    for i, key in enumerate(keys, 1):
        print(f"  {i}) {key:16} - {profiles[key]['description']}")
    print()
    raw = input(f"Choose [1-{len(keys)}] (default 2=fleet): ").strip() or "2"
    try:
        idx = int(raw) - 1
        return keys[idx]
    except (ValueError, IndexError):
        raise SystemExit("Invalid choice") from None


def bootstrap(
    *,
    profile: str | None = None,
    packages: list[str] | None = None,
    workspace: Path | None = None,
    install: bool = True,
    interactive: bool = False,
) -> dict[str, Any]:
    root = find_workspace_root()
    if workspace is None:
        if root is not None:
            workspace = root
        elif (Path.cwd() / "bot.yaml").exists() or (Path.cwd() / "orchestrator.yaml").exists():
            # Running from a standalone package clone - siblings go next to it
            workspace = Path.cwd().resolve().parent
        else:
            workspace = Path.cwd().resolve()

    workspace = Path(workspace).resolve()
    workspace.mkdir(parents=True, exist_ok=True)

    # Load manifest from monorepo root or vendored package copy
    try:
        manifest = load_manifest(workspace)
    except FileNotFoundError:
        print("-> No local manifest - seeding workspace from platform repo")
        _seed_workspace(workspace)
        manifest = load_manifest(workspace)

    if interactive and not profile and not packages:
        profile = interactive_profile(manifest)

    selected = resolve_packages(profile, packages, manifest)
    print(f"\nPackages: {', '.join(selected)}\n")
    paths = ensure_packages(selected, workspace=workspace, manifest=manifest)

    if "orchestrator" in selected:
        write_orchestrator_yaml(workspace, selected, manifest)

    copy_env_examples(workspace, selected, manifest)

    if install:
        venv = create_venv(workspace)
        py = venv_python(venv)
        run([str(py), "-m", "pip", "install", "-U", "pip", "setuptools", "wheel"])
        # install pyyaml into venv for future runs
        run([str(py), "-m", "pip", "install", "pyyaml"])
        pip_install_editable(py, paths)
        if "bot-instagram" in selected:
            print("-> Installing Playwright Chromium for Instagram bot")
            run([str(py), "-m", "playwright", "install", "chromium"], check=False)

    profile_meta = manifest["profiles"].get(profile or "fleet", {})
    open_target = profile_meta.get("open", ".")
    open_path = (workspace / open_target).resolve() if open_target != "." else workspace

    print("\n======== Setup complete ========")
    print(f"Workspace : {workspace}")
    print(f"Open in Cursor / VS Code:")
    print(f"  {open_path}")
    if "orchestrator" in selected:
        print("Start dashboard:")
        print("  .\\.venv\\Scripts\\Activate.ps1   # Windows")
        print("  source .venv/bin/activate      # macOS/Linux")
        print("  python -m orchestrator_app.main")
        print("  -> http://127.0.0.1:7400")
    if profile_meta.get("run_hint"):
        print(f"Hint: {profile_meta['run_hint']}")
    print("================================\n")
    return {
        "workspace": str(workspace),
        "packages": selected,
        "open": str(open_path),
        "profile": profile,
    }


def _seed_workspace(workspace: Path) -> None:
    """Clone platform repo into workspace if empty / missing packages."""
    workspace.mkdir(parents=True, exist_ok=True)
    # Prefer git sparse clone of just the manifest + scripts
    if any(workspace.iterdir()) and not (workspace / "platform.manifest.yaml").exists():
        # Workspace has some packages already — fetch manifest file only via git archive-like clone
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp) / "repo"
            run(
                [
                    "git",
                    "clone",
                    "--filter=blob:none",
                    "--sparse",
                    "--depth",
                    "1",
                    "https://github.com/kashewknutt/social-agent-platform.git",
                    str(tmp_path),
                ]
            )
            run(["git", "sparse-checkout", "set", "platform.manifest.yaml", "scripts"], cwd=tmp_path)
            shutil.copy(tmp_path / "platform.manifest.yaml", workspace / "platform.manifest.yaml")
            if (tmp_path / "scripts").exists():
                shutil.copytree(tmp_path / "scripts", workspace / "scripts", dirs_exist_ok=True)
        return

    if not (workspace / ".git").exists() and not (workspace / "platform.manifest.yaml").exists():
        run(
            [
                "git",
                "clone",
                "--depth",
                "1",
                "https://github.com/kashewknutt/social-agent-platform.git",
                str(workspace / "_platform_tmp"),
            ]
        )
        tmp = workspace / "_platform_tmp"
        for item in tmp.iterdir():
            target = workspace / item.name
            if not target.exists():
                shutil.move(str(item), str(target))
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Bootstrap social-agent platform packages")
    parser.add_argument("--profile", choices=["core", "instagram", "fleet"], help="Preset package set")
    parser.add_argument("--packages", nargs="*", help="Explicit package names")
    parser.add_argument("--workspace", type=Path, help="Parent folder for packages")
    parser.add_argument("--no-install", action="store_true", help="Only clone/copy packages")
    parser.add_argument("-i", "--interactive", action="store_true", help="Ask which profile to install")
    args = parser.parse_args(argv)
    bootstrap(
        profile=args.profile,
        packages=args.packages,
        workspace=args.workspace,
        install=not args.no_install,
        interactive=args.interactive or (not args.profile and not args.packages),
    )


if __name__ == "__main__":
    main()
