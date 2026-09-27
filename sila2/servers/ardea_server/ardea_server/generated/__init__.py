# The nine SiLA feature packages the real Ardea server exposes, copied verbatim from it
# (four Ardea-specific ones from `ardea-sila2`, three b-CAP and two KV COM+ ones from the
# provider repositories it vendors). Each subpackage holds one feature's
# base/client/errors/feature/types modules plus its .sila.xml and .proto -- and that .sila.xml
# is what the server serves, so copying it is what makes this mock's feature definitions
# byte-identical to the real machine's. See `specs/ardea_server/README.md`.
