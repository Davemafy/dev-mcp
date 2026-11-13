"""Safe Nginx cutover keeps the Refit host and cert separate."""
import pathlib

def test_native_nginx_setup_has_safe_tls_preconditions():
    script=(pathlib.Path(__file__).resolve().parents[1]/"deploy"/"configure-native-nginx.sh").read_text()
    assert "devmcp-native.conf" in script
    assert "nginx -t" in script
    assert "systemctl reload nginx" in script
    assert "certbot certonly --webroot" in script
    assert '--resolve "$domain:443:127.0.0.1"' in script
    assert '127.0.0.1:8742' in script
    assert "proxy_buffering off" in script
    assert "refit-platform-ip" not in script
    assert "docker" not in script
