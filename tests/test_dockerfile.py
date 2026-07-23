from pathlib import Path


def test_frontend_dependency_install_pins_pnpm_and_scopes_fallback() -> None:
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")

    assert "COPY admin-ui/package.json admin-ui/pnpm-lock.yaml ./" in dockerfile
    assert "corepack prepare pnpm@10.34.5 --activate" in dockerfile
    assert "&& pnpm install --frozen-lockfile" in dockerfile
    assert "|| pnpm install" not in dockerfile
