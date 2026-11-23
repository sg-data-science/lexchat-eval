Put a CA bundle here only if AILA's instance reaches the internet through a
TLS-inspecting proxy, then set `SSL_CERT_FILE=/etc/lexeval/ca/<file>` in the
settings file. The bundle replaces the trusted certificates, so it must hold the
usual public ones as well as the proxy's own. Files here are never committed.
