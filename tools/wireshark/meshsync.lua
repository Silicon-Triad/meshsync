-- Wireshark / tshark dissector for the meshsync protocol (#82, D5 as changed by #81; R5).
-- Owner: Muhammad Ahmed Qazi (protocol owner, tools/wireshark/); written under #82 by
-- Mujtaba Jawaid Rao. Every value below is copied from docs/PROTOCOL.md and
-- src/meshsync/protocol/constants.py (R-10.7); keep them in step with those files.
--
-- Install: copy to ~/.local/lib/wireshark/plugins/ (Wireshark >= 3.x), or run
--   tshark -X lua_script:tools/wireshark/meshsync.lua -r capture.pcapng -Y meshsync
--
-- TCP (PROTOCOL.md §4.1): 12-byte header "!2sBBII" = MAGIC "MS", VER, TYPE, REQ_ID, BODY_LEN,
-- then BODY (UTF-8 JSON, raw bytes for CHUNK_DATA). Messages are reassembled from BODY_LEN with
-- dissect_tcp_pdus, so a message split across segments, or several in one, decode correctly.
-- UDP (PROTOCOL.md §3.1): one JSON object per datagram (ANNOUNCE / LEAVE).

local MAGIC = "MS"                -- constants.MAGIC
local VERSION = 1                 -- constants.VERSION
local HEADER_SIZE = 12            -- constants.HEADER_SIZE
local MAX_BODY = 1024 * 1024      -- constants.MAX_BODY (1 MiB)
local TCP_PORT = 47001            -- constants.TCP_PORT
local UDP_PORT = 47000            -- constants.UDP_PORT
local CHUNK_DATA = 0x21

local msg_types = {               -- constants.MsgType, PROTOCOL.md §5
  [0x01] = "HELLO", [0x02] = "HELLO_ACK", [0x03] = "AUTH", [0x04] = "AUTH_OK",
  [0x10] = "INDEX_REQ", [0x11] = "INDEX", [0x12] = "NOTIFY",
  [0x20] = "CHUNK_REQ", [0x21] = "CHUNK_DATA",
  [0x30] = "PING", [0x31] = "PONG",
  [0x7E] = "ERROR", [0x7F] = "BYE",
}
local error_codes = {             -- constants.ErrorCode, PROTOCOL.md §10
  [0x01] = "BAD_HEADER", [0x02] = "BAD_VERSION", [0x03] = "ILLEGAL_STATE",
  [0x04] = "AUTH_FAILED", [0x05] = "TOO_LARGE", [0x10] = "CHUNK_NOT_FOUND",
  [0x11] = "CHUNK_CORRUPT", [0x20] = "BUSY", [0x7F] = "INTERNAL",
}

local p = Proto("meshsync", "meshsync Protocol")

local f = p.fields
f.magic = ProtoField.string("meshsync.magic", "Magic")
f.ver = ProtoField.uint8("meshsync.ver", "Version")
f.type = ProtoField.uint8("meshsync.type", "Type", base.HEX, msg_types)
f.req_id = ProtoField.uint32("meshsync.req_id", "Request ID")
f.body_len = ProtoField.uint32("meshsync.body_len", "Body length")
f.body = ProtoField.bytes("meshsync.body", "Body")
f.chunk = ProtoField.bytes("meshsync.chunk", "Chunk data")
f.err_code = ProtoField.uint8("meshsync.error.code", "Error code", base.HEX, error_codes)
f.err_msg = ProtoField.string("meshsync.error.msg", "Error message")
f.d_type = ProtoField.string("meshsync.disc.type", "Discovery type")
f.d_name = ProtoField.string("meshsync.disc.name", "Name")
f.d_peer = ProtoField.string("meshsync.disc.peer_id", "Peer ID")
f.d_port = ProtoField.uint16("meshsync.disc.tcp_port", "TCP port")
f.d_rev = ProtoField.uint32("meshsync.disc.index_rev", "Index revision")
f.d_ts = ProtoField.uint32("meshsync.disc.ts", "Timestamp")
f.d_tag = ProtoField.string("meshsync.disc.tag", "HMAC tag")

local e_magic = ProtoExpert.new("meshsync.bad_magic", "Bad MAGIC (expected \"MS\")",
                                expert.group.MALFORMED, expert.severity.ERROR)
local e_ver = ProtoExpert.new("meshsync.bad_version", "Unknown VER",
                              expert.group.MALFORMED, expert.severity.ERROR)
local e_type = ProtoExpert.new("meshsync.bad_type", "Unknown TYPE",
                               expert.group.MALFORMED, expert.severity.WARN)
local e_large = ProtoExpert.new("meshsync.too_large", "BODY_LEN above MAX_BODY (1 MiB)",
                                expert.group.MALFORMED, expert.severity.ERROR)
local e_trunc = ProtoExpert.new("meshsync.truncated", "Body cut off in this capture (snaplen)",
                                expert.group.UNDECODED, expert.severity.NOTE)
p.experts = { e_magic, e_ver, e_type, e_large, e_trunc }

local json = Dissector.get("json")

-- Hand a JSON body to Wireshark's json dissector for the field tree, but keep our columns.
local function call_json(tvb, pinfo, tree)
  if not json then return end
  local info = tostring(pinfo.cols.info)
  pcall(json.call, json, tvb, pinfo, tree)
  pinfo.cols.protocol:set("MESHSYNC")
  pinfo.cols.info:set(info)
end

-- Value of a top-level JSON key (string or number), or nil. Enough for our flat bodies.
local function json_field(s, key)
  local v = s:match('"' .. key .. '"%s*:%s*"([^"]*)"')
  if v then return v end
  return s:match('"' .. key .. '"%s*:%s*(%-?[%d%.]+)')
end

local function add_info(pinfo, text)
  local cur = tostring(pinfo.cols.info)
  if cur:find("meshsync:", 1, true) then
    pinfo.cols.info:append(", " .. text)
  else
    pinfo.cols.info:set("meshsync: " .. text)
  end
end

-- A plausible meshsync header at the start of tvb: MAGIC, VER and a known TYPE.
local function looks_like_header(tvb)
  return tvb:captured_len() >= HEADER_SIZE and tvb(0, 2):string() == MAGIC
     and tvb(2, 1):uint() == VERSION and msg_types[tvb(3, 1):uint()] ~= nil
end

-- One complete (or, in a snaplen-limited capture, cut-off) TCP message at the start of tvb.
local function dissect_msg(tvb, pinfo, tree)
  pinfo.cols.protocol:set("MESHSYNC")
  local avail = tvb:captured_len()
  local t = tvb(3, 1):uint()
  local name = msg_types[t] or string.format("0x%02x", t)
  local body_len = tvb(8, 4):uint()
  local req_id = tvb(4, 4):uint()
  local sub = tree:add(p, tvb(0, math.min(avail, HEADER_SIZE + body_len)),
                       "meshsync " .. name)
  local m = sub:add(f.magic, tvb(0, 2))
  if tvb(0, 2):string() ~= MAGIC then m:add_proto_expert_info(e_magic) end
  local v = sub:add(f.ver, tvb(2, 1))
  if tvb(2, 1):uint() ~= VERSION then v:add_proto_expert_info(e_ver) end
  local ti = sub:add(f.type, tvb(3, 1))
  if not msg_types[t] then ti:add_proto_expert_info(e_type) end
  sub:add(f.req_id, tvb(4, 4))
  local bl = sub:add(f.body_len, tvb(8, 4))
  if body_len > MAX_BODY then bl:add_proto_expert_info(e_large) end
  add_info(pinfo, name .. " req_id=" .. req_id)

  local have = math.min(body_len, avail - HEADER_SIZE)
  if have <= 0 then
    if body_len > 0 then sub:add_proto_expert_info(e_trunc) end
    return
  end
  local body = tvb(HEADER_SIZE, have)
  if t == CHUNK_DATA then
    sub:add(f.chunk, body)
  else
    sub:add(f.body, body)
    local s = body:string()
    if t == 0x7E then                       -- ERROR: show the code by name
      local code = tonumber(json_field(s, "code") or "")
      if code then
        sub:add(f.err_code, body, code)
        add_info(pinfo, "code=" .. (error_codes[code] or code))
      end
      local msg = json_field(s, "msg")
      if msg then sub:add(f.err_msg, body, msg) end
    end
    if have == body_len then call_json(body:tvb(), pinfo, sub) end
  end
  if have < body_len then sub:add_proto_expert_info(e_trunc) end
end

local function msg_len(tvb, pinfo, offset)
  return HEADER_SIZE + tvb(offset + 8, 4):uint()
end

function p.dissector(tvb, pinfo, tree)
  if tvb:captured_len() < tvb:reported_length_remaining() then
    -- Snaplen-limited segment: TCP cannot reassemble it, so decode the header we have, if the
    -- segment starts with one (a segment can also start inside a CHUNK_DATA body).
    if looks_like_header(tvb) then
      dissect_msg(tvb, pinfo, tree)
      return tvb:captured_len()
    end
    return 0
  end
  dissect_tcp_pdus(tvb, tree, HEADER_SIZE, msg_len, dissect_msg)
  return tvb:len()
end

-- Heuristic: other --tcp-port values (e.g. run_local_mesh.sh uses 47002/47003).
local function heuristic(tvb, pinfo, tree)
  if not looks_like_header(tvb) then return false end
  pinfo.conversation = p
  p.dissector(tvb, pinfo, tree)
  return true
end

-- UDP discovery (PROTOCOL.md §3.1): one JSON object per datagram.
local pd = Proto("meshsync_disc", "meshsync Discovery")
function pd.dissector(tvb, pinfo, tree)
  local s = tvb():string()
  if not s:find('"magic"%s*:%s*"MS"') then return 0 end
  pinfo.cols.protocol:set("MESHSYNC")
  local sub = tree:add(p, tvb(), "meshsync discovery")
  local dtype = json_field(s, "type") or "?"
  local fields = {
    { f.d_type, "type" }, { f.d_name, "name" }, { f.d_peer, "peer_id" },
    { f.d_port, "tcp_port", true }, { f.d_rev, "index_rev", true }, { f.d_ts, "ts", true },
    { f.d_tag, "tag" },
  }
  for _, fd in ipairs(fields) do
    local val = json_field(s, fd[2])
    if val then sub:add(fd[1], tvb(), fd[3] and tonumber(val) or val) end
  end
  pinfo.cols.info:set("meshsync: " .. dtype .. " " .. (json_field(s, "name") or "?")
                      .. " peer_id=" .. (json_field(s, "peer_id") or "?"))
  call_json(tvb, pinfo, sub)
  return tvb:len()
end

DissectorTable.get("tcp.port"):add(TCP_PORT, p)
DissectorTable.get("udp.port"):add(UDP_PORT, pd)
p:register_heuristic("tcp", heuristic)
