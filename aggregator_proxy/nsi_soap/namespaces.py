"""XML namespace map for NSI CS v2 SOAP messages."""

NSMAP: dict[str, str] = {
    "soapenv": "http://schemas.xmlsoap.org/soap/envelope/",
    "nsi_headers": "http://schemas.ogf.org/nsi/2013/12/framework/headers",
    "nsi_ftypes": "http://schemas.ogf.org/nsi/2013/12/framework/types",
    "nsi_ctypes": "http://schemas.ogf.org/nsi/2013/12/connection/types",
    "nsi_p2p": "http://schemas.ogf.org/nsi/2013/12/services/point2point",
    "nsi_stypes": "http://schemas.ogf.org/nsi/2013/12/services/types",
    "path_trace": "http://schemas.ogf.org/nsi/2015/04/connection/pathtrace",
    "saml": "urn:oasis:names:tc:SAML:2.0:assertion",
}
