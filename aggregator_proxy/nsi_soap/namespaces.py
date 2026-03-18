# Copyright 2026 SURF
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


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
