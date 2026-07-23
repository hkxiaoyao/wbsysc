from pathlib import Path


def test_compose_exposes_application_only_on_host_loopback() -> None:
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")

    assert '"127.0.0.1:8001:8001"' in compose
    assert '"8001:8001"' not in compose


def test_nginx_https_routes_forward_original_scheme() -> None:
    nginx = Path("deploy/nginx.conf").read_text(encoding="utf-8")

    assert "return 301 https://$host$request_uri;" in nginx
    assert "location ^~ /tenant/" in nginx
    assert "location / {" in nginx
    assert nginx.count("proxy_set_header X-Forwarded-Proto $scheme;") >= 7


def test_bare_metal_service_trusts_only_local_nginx() -> None:
    service = Path("deploy/wbsysc.service").read_text(encoding="utf-8")

    assert "--proxy-headers --forwarded-allow-ips 127.0.0.1" in service
    assert "--forwarded-allow-ips *" not in service
