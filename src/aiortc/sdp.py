import ipaddress
import re
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple

import attr

from . import rtp
from .rtcdtlstransport import RTCDtlsFingerprint, RTCDtlsParameters
from .rtcicetransport import RTCIceCandidate, RTCIceParameters
from .rtcrtpparameters import (
    RTCRtcpFeedback,
    RTCRtpCodecParameters,
    RTCRtpHeaderExtensionParameters,
    RTCRtpParameters,
)
from .rtcsctptransport import RTCSctpCapabilities

DIRECTIONS = ["inactive", "sendonly", "recvonly", "sendrecv"]

DTLS_ROLE_SETUP = {"auto": "actpass", "client": "active", "server": "passive"}
DTLS_SETUP_ROLE = dict([(v, k) for (k, v) in DTLS_ROLE_SETUP.items()])

FMTP_INT_PARAMETERS = [
    "apt",
    "max-fr",
    "max-fs",
    "maxplaybackrate",
    "minptime",
    "stereo",
    "useinbandfec",
]


def candidate_from_sdp(sdp: str) -> RTCIceCandidate:
    bits = sdp.split()
    assert len(bits) >= 8

    candidate = RTCIceCandidate(
        component=int(bits[1]),
        foundation=bits[0],
        ip=bits[4],
        port=int(bits[5]),
        priority=int(bits[3]),
        protocol=bits[2],
        type=bits[7],
    )

    for i in range(8, len(bits) - 1, 2):
        if bits[i] == "raddr":
            candidate.relatedAddress = bits[i + 1]
        elif bits[i] == "rport":
            candidate.relatedPort = int(bits[i + 1])
        elif bits[i] == "tcptype":
            candidate.tcpType = bits[i + 1]

    return candidate


def candidate_to_sdp(candidate: RTCIceCandidate) -> str:
    sdp = "%s %d %s %d %s %d typ %s" % (
        candidate.foundation,
        candidate.component,
        candidate.protocol,
        candidate.priority,
        candidate.ip,
        candidate.port,
        candidate.type,
    )

    if candidate.relatedAddress is not None:
        sdp += " raddr %s" % candidate.relatedAddress
    if candidate.relatedPort is not None:
        sdp += " rport %s" % candidate.relatedPort
    if candidate.tcpType is not None:
        sdp += " tcptype %s" % candidate.tcpType
    return sdp


def grouplines(sdp: str) -> Tuple[List[str], List[List[str]]]:
    session = []
    media = []
    for line in sdp.splitlines():
        if line.startswith("m="):
            media.append([line])
        elif len(media):
            media[-1].append(line)
        else:
            session.append(line)
    return session, media


def ipaddress_from_sdp(sdp: str) -> str:
    m = re.match("^IN (IP4|IP6) ([^ ]+)$", sdp)
    assert m
    return m.group(2)


def ipaddress_to_sdp(addr: str) -> str:
    version = ipaddress.ip_address(addr).version
    return "IN IP%d %s" % (version, addr)


def parameters_from_sdp(sdp: str) -> OrderedDict:
    parameters = OrderedDict()  # type: OrderedDict
    for param in sdp.split(";"):
        if "=" in param:
            k, v = param.split("=", 1)
            if k in FMTP_INT_PARAMETERS:
                parameters[k] = int(v)
            else:
                parameters[k] = v
        else:
            parameters[param] = None
    return parameters


def parameters_to_sdp(parameters: OrderedDict) -> str:
    params = []
    for param_k, param_v in parameters.items():
        if param_v is not None:
            params.append("%s=%s" % (param_k, param_v))
        else:
            params.append(param_k)
    return ";".join(params)


def parse_attr(line: str) -> Tuple[str, Optional[str]]:
    if ":" in line:
        bits = line[2:].split(":", 1)
        return bits[0], bits[1]
    else:
        return line[2:], None


@attr.s
class GroupDescription:
    semantic = attr.ib()  # type: str
    items = attr.ib()  # List[Union[int, str]]

    def __str__(self) -> str:
        return "%s %s" % (self.semantic, " ".join(map(str, self.items)))


def parse_group(dest: List[GroupDescription], value: str, type=str) -> None:
    bits = value.split()
    if bits:
        dest.append(GroupDescription(semantic=bits[0], items=list(map(type, bits[1:]))))


@attr.s
class SsrcDescription:
    ssrc = attr.ib()
    cname = attr.ib(default=None)
    msid = attr.ib(default=None)
    mslabel = attr.ib(default=None)
    label = attr.ib(default=None)


SSRC_INFO_ATTRS = ["cname", "msid", "mslabel", "label"]


class MediaDescription:
    def __init__(self, kind: str, port: int, profile: str, fmt: List[Any]) -> None:
        # rtp
        self.kind = kind
        self.port = port
        self.host = None  # type: Optional[str]
        self.profile = profile
        self.direction = None  # type: Optional[str]
        self.msid = None  # type: Optional[str]

        # rtcp
        self.rtcp_port = None  # type: Optional[int]
        self.rtcp_host = None  # type: Optional[str]
        self.rtcp_mux = False

        # ssrc
        self.ssrc = []  # type: List[SsrcDescription]
        self.ssrc_group = []  # type: List[GroupDescription]

        # formats
        self.fmt = fmt
        self.rtp = RTCRtpParameters()

        # SCTP
        self.sctpCapabilities = None  # type: Optional[RTCSctpCapabilities]
        self.sctpmap = {}  # type: Dict[int, str]
        self.sctp_port = None  # type: Optional[int]

        # DTLS
        self.dtls = None  # type: Optional[RTCDtlsParameters]

        # ICE
        self.ice = RTCIceParameters()
        self.ice_candidates = []  # type: List[RTCIceCandidate]
        self.ice_candidates_complete = False
        self.ice_options = None  # type: Optional[str]

    def __str__(self) -> str:
        lines = []
        lines.append(
            "m=%s %d %s %s"
            % (self.kind, self.port, self.profile, " ".join(map(str, self.fmt)))
        )
        if self.host is not None:
            lines.append("c=%s" % ipaddress_to_sdp(self.host))
        if self.direction is not None:
            lines.append("a=" + self.direction)

        for header in self.rtp.headerExtensions:
            lines.append("a=extmap:%d %s" % (header.id, header.uri))

        if self.rtp.muxId:
            lines.append("a=mid:" + self.rtp.muxId)

        if self.msid:
            lines.append("a=msid:" + self.msid)

        if self.rtcp_port is not None and self.rtcp_host is not None:
            lines.append(
                "a=rtcp:%d %s" % (self.rtcp_port, ipaddress_to_sdp(self.rtcp_host))
            )
            if self.rtcp_mux:
                lines.append("a=rtcp-mux")

        for group in self.ssrc_group:
            lines.append("a=ssrc-group:%s" % group)
        for ssrc_info in self.ssrc:
            for ssrc_attr in SSRC_INFO_ATTRS:
                ssrc_value = getattr(ssrc_info, ssrc_attr)
                if ssrc_value is not None:
                    lines.append(
                        "a=ssrc:%d %s:%s" % (ssrc_info.ssrc, ssrc_attr, ssrc_value)
                    )

        for codec in self.rtp.codecs:
            lines.append("a=rtpmap:%d %s" % (codec.payloadType, codec))

            # RTCP feedback
            for feedback in codec.rtcpFeedback:
                value = feedback.type
                if feedback.parameter:
                    value += " " + feedback.parameter
                lines.append("a=rtcp-fb:%d %s" % (codec.payloadType, value))

            # parameters
            params = parameters_to_sdp(codec.parameters)
            if params:
                lines.append("a=fmtp:%d %s" % (codec.payloadType, params))

        for k, v in self.sctpmap.items():
            lines.append("a=sctpmap:%d %s" % (k, v))
        if self.sctp_port is not None:
            lines.append("a=sctp-port:%d" % self.sctp_port)
        if self.sctpCapabilities is not None:
            lines.append("a=max-message-size:%d" % self.sctpCapabilities.maxMessageSize)

        # ice
        for candidate in self.ice_candidates:
            lines.append("a=candidate:" + candidate_to_sdp(candidate))
        if self.ice_candidates_complete:
            lines.append("a=end-of-candidates")
        if self.ice.usernameFragment is not None:
            lines.append("a=ice-ufrag:" + self.ice.usernameFragment)
        if self.ice.password is not None:
            lines.append("a=ice-pwd:" + self.ice.password)
        if self.ice_options is not None:
            lines.append("a=ice-options:" + self.ice_options)

        # dtls
        if self.dtls:
            for fingerprint in self.dtls.fingerprints:
                lines.append(
                    "a=fingerprint:%s %s" % (fingerprint.algorithm, fingerprint.value)
                )
            lines.append("a=setup:" + DTLS_ROLE_SETUP[self.dtls.role])

        return "\r\n".join(lines) + "\r\n"


class SessionDescription:
    def __init__(self) -> None:
        self.version = 0
        self.origin = None  # type: Optional[str]
        self.name = "-"
        self.time = "0 0"
        self.host = None  # type: Optional[str]
        self.group = []  # type: List[GroupDescription]
        self.msid_semantic = []  # type: List[GroupDescription]
        self.media = []  # type: List[MediaDescription]
        self.type = None  # type: str

    @classmethod
    def parse(cls, sdp: str):
        current_media = None  # type: Optional[MediaDescription]
        dtls_fingerprints = []
        ice_options = None

        def find_codec(pt: int) -> RTCRtpCodecParameters:
            return next(filter(lambda x: x.payloadType == pt, current_media.rtp.codecs))

        session_lines, media_groups = grouplines(sdp)

        # parse session
        session = cls()
        for line in session_lines:
            if line.startswith("v="):
                session.version = int(line.strip()[2:])
            elif line.startswith("o="):
                session.origin = line.strip()[2:]
            elif line.startswith("s="):
                session.name = line.strip()[2:]
            elif line.startswith("c="):
                session.host = ipaddress_from_sdp(line[2:])
            elif line.startswith("t="):
                session.time = line.strip()[2:]
            elif line.startswith("a="):
                attr, value = parse_attr(line)
                if attr == "fingerprint":
                    algorithm, fingerprint = value.split()
                    dtls_fingerprints.append(
                        RTCDtlsFingerprint(algorithm=algorithm, value=fingerprint)
                    )
                elif attr == "ice-options":
                    ice_options = value
                elif attr == "group":
                    parse_group(session.group, value)
                elif attr == "msid-semantic":
                    parse_group(session.msid_semantic, value)

        # parse media
        for media_lines in media_groups:
            m = re.match("^m=([^ ]+) ([0-9]+) ([A-Z/]+) (.+)$", media_lines[0])
            assert m

            # check payload types are valid
            kind = m.group(1)
            fmt = m.group(4).split()
            fmt_int = None  # type: Optional[List[int]]
            if kind in ["audio", "video"]:
                fmt_int = [int(x) for x in fmt]
                for pt in fmt_int:
                    assert pt >= 0 and pt < 256
                    assert pt not in rtp.FORBIDDEN_PAYLOAD_TYPES

            current_media = MediaDescription(
                kind=kind, port=int(m.group(2)), profile=m.group(3), fmt=fmt_int or fmt
            )
            current_media.dtls = RTCDtlsParameters(
                fingerprints=dtls_fingerprints[:], role=None
            )
            current_media.ice_options = ice_options
            session.media.append(current_media)

            for line in media_lines[1:]:
                if line.startswith("c="):
                    current_media.host = ipaddress_from_sdp(line[2:])
                elif line.startswith("a="):
                    attr, value = parse_attr(line)
                    if attr == "candidate":
                        current_media.ice_candidates.append(candidate_from_sdp(value))
                    elif attr == "end-of-candidates":
                        current_media.ice_candidates_complete = True
                    elif attr == "extmap":
                        ext_id, ext_uri = value.split()
                        if "/" in ext_id:
                            ext_id, ext_direction = ext_id.split("/")
                        extension = RTCRtpHeaderExtensionParameters(
                            id=int(ext_id), uri=ext_uri
                        )
                        current_media.rtp.headerExtensions.append(extension)
                    elif attr == "fingerprint":
                        algorithm, fingerprint = value.split()
                        current_media.dtls.fingerprints.append(
                            RTCDtlsFingerprint(algorithm=algorithm, value=fingerprint)
                        )
                    elif attr == "ice-ufrag":
                        current_media.ice.usernameFragment = value
                    elif attr == "ice-pwd":
                        current_media.ice.password = value
                    elif attr == "ice-options":
                        current_media.ice_options = value
                    elif attr == "max-message-size":
                        current_media.sctpCapabilities = RTCSctpCapabilities(
                            maxMessageSize=int(value)
                        )
                    elif attr == "mid":
                        current_media.rtp.muxId = value
                    elif attr == "msid":
                        current_media.msid = value
                    elif attr == "rtcp":
                        port, rest = value.split(" ", 1)
                        current_media.rtcp_port = int(port)
                        current_media.rtcp_host = ipaddress_from_sdp(rest)
                    elif attr == "rtcp-mux":
                        current_media.rtcp_mux = True
                    elif attr == "setup":
                        current_media.dtls.role = DTLS_SETUP_ROLE[value]
                    elif attr in DIRECTIONS:
                        current_media.direction = attr
                    elif attr == "rtpmap":
                        format_id, format_desc = value.split(" ", 1)
                        bits = format_desc.split("/")
                        if current_media.kind == "audio":
                            if len(bits) > 2:
                                channels = int(bits[2])
                            else:
                                channels = 1
                        else:
                            channels = None
                        codec = RTCRtpCodecParameters(
                            mimeType=current_media.kind + "/" + bits[0],
                            channels=channels,
                            clockRate=int(bits[1]),
                            payloadType=int(format_id),
                        )
                        current_media.rtp.codecs.append(codec)
                    elif attr == "sctpmap":
                        format_id, format_desc = value.split(" ", 1)
                        getattr(current_media, attr)[int(format_id)] = format_desc
                    elif attr == "sctp-port":
                        current_media.sctp_port = int(value)
                    elif attr == "ssrc-group":
                        parse_group(current_media.ssrc_group, value, type=int)
                    elif attr == "ssrc":
                        ssrc_str, ssrc_desc = value.split(" ", 1)
                        ssrc = int(ssrc_str)
                        ssrc_attr, ssrc_value = ssrc_desc.split(":")

                        try:
                            ssrc_info = next(
                                (x for x in current_media.ssrc if x.ssrc == ssrc)
                            )
                        except StopIteration:
                            ssrc_info = SsrcDescription(ssrc=ssrc)
                            current_media.ssrc.append(ssrc_info)
                        if ssrc_attr in SSRC_INFO_ATTRS:
                            setattr(ssrc_info, ssrc_attr, ssrc_value)

            if current_media.dtls.role is None:
                current_media.dtls = None

            # requires codecs to have been parsed
            for line in media_lines[1:]:
                if line.startswith("a="):
                    attr, value = parse_attr(line)
                    if attr == "fmtp":
                        format_id, format_desc = value.split(" ", 1)
                        codec = find_codec(int(format_id))
                        codec.parameters = parameters_from_sdp(format_desc)
                    elif attr == "rtcp-fb":
                        bits = value.split(" ", 2)
                        codec = find_codec(int(bits[0]))
                        codec.rtcpFeedback.append(
                            RTCRtcpFeedback(
                                type=bits[1],
                                parameter=bits[2] if len(bits) > 2 else None,
                            )
                        )

        return session

    def __str__(self) -> str:
        lines = ["v=%d" % self.version, "o=%s" % self.origin, "s=%s" % self.name]
        if self.host is not None:
            lines += ["c=%s" % ipaddress_to_sdp(self.host)]
        lines += ["t=%s" % self.time]
        for group in self.group:
            lines += ["a=group:%s" % group]
        for group in self.msid_semantic:
            lines += ["a=msid-semantic:%s" % group]
        return "\r\n".join(lines) + "\r\n" + "".join([str(m) for m in self.media])
