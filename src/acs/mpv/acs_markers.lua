--[[
Anime Clip Studio — markers from the stand-alone mpv player.

While watching, press (defaults; Anime Clip Studio writes the keys you chose at install time):
  Alt+m          add a marker at the current frame
  Alt+i / Alt+o  mark a clip start / clip end
  Alt+u          undo the last mark made for this file
  Alt+l          list the marks of this file on screen

Every action is appended as one JSON line to
  %APPDATA%\AnimeClipStudio\mpv-markers.jsonl
(or, with store=sidecar, to "<video>.acsmarks.jsonl" next to the video).
When the video is opened in Anime Clip Studio it offers to import these marks.

Keys can be rebound in input.conf, e.g.:  Ctrl+m script-binding acs_markers/acs-marker
Options (script-opts/acs_markers.conf):   store=central|sidecar   file=<custom central file>
                                          key_marker=… key_clip_start=… key_clip_end=… key_undo=… key_list=…
]]

local mp = require "mp"
local utils = require "mp.utils"
local msg = require "mp.msg"

local opts = {
    store = "central",
    file = "",
    key_marker = "Alt+m",
    key_clip_start = "Alt+i",
    key_clip_end = "Alt+o",
    key_undo = "Alt+u",
    key_list = "Alt+l",
}
require("mp.options").read_options(opts, "acs_markers")

local current = nil   -- { path, filename, size, entries = { {id, kind, time, created}, ... } }
local KIND_LABEL = { marker = "Marker", ["in"] = "Clip start", out = "Clip end" }

local function central_file()
    if opts.file ~= "" then return opts.file end
    local appdata = os.getenv("APPDATA")
    if not appdata then return nil end
    return appdata .. "\\AnimeClipStudio\\mpv-markers.jsonl"
end

local function store_file()
    if opts.store == "sidecar" and current then return current.path .. ".acsmarks.jsonl" end
    return central_file()
end

local function is_absolute(p)
    return p:match("^%a:[/\\]") or p:match("^[/\\][/\\]") or p:match("^/") or p:match("://")
end

local function norm(p)
    return (p or ""):gsub("/", "\\"):lower()
end

local function fmt_time(t)
    local ms = math.floor(t * 1000 + 0.5)
    local h = math.floor(ms / 3600000)
    local m = math.floor(ms % 3600000 / 60000)
    local s = math.floor(ms % 60000 / 1000)
    return string.format("%02d:%02d:%02d.%03d", h, m, s, ms % 1000)
end

local function osd(text, seconds)
    mp.osd_message("ACS · " .. text, seconds or 2.5)
end

local function same_file(rec)
    if not current then return false end
    if rec.path and norm(rec.path) == norm(current.path) then return true end
    return rec.filename and current.size and rec.size
        and rec.filename:lower() == current.filename:lower() and rec.size == current.size
end

local function read_lines(file, fn)
    local f = io.open(file, "r")
    if not f then return end
    for line in f:lines() do
        local rec = line ~= "" and utils.parse_json(line) or nil
        if type(rec) == "table" then fn(rec) end
    end
    f:close()
end

local function load_entries()
    local by_id, order = {}, {}
    local function take(rec)
        if not same_file(rec) then return end
        if rec.op == "add" and rec.id then
            by_id[rec.id] = { id = rec.id, kind = rec.kind, time = rec.time, created = rec.created or 0 }
            table.insert(order, rec.id)
        elseif rec.op == "del" and rec.target then
            by_id[rec.target] = nil
        end
    end
    local central = central_file()
    if central then read_lines(central, take) end
    if current then read_lines(current.path .. ".acsmarks.jsonl", take) end
    local list = {}
    for _, id in ipairs(order) do
        if by_id[id] then table.insert(list, by_id[id]); by_id[id] = nil end
    end
    return list
end

local function append(rec)
    local file = store_file()
    if not file then osd("cannot find %APPDATA%", 4); return false end
    local f, err = io.open(file, "a")
    if not f then
        msg.error("cannot write " .. file .. ": " .. tostring(err))
        osd("cannot write the marks file — start Anime Clip Studio once", 4)
        return false
    end
    f:write(utils.format_json(rec) .. "\n")
    f:close()
    return true
end

local function on_file_loaded()
    local path = mp.get_property("path")
    if not path or path:match("://") then current = nil; return end
    if not is_absolute(path) then path = utils.join_path(utils.getcwd(), path) end
    path = path:gsub("/", "\\")
    current = {
        path = path,
        filename = mp.get_property("filename") or path:match("[^\\]+$"),
        size = mp.get_property_number("file-size"),
        entries = {},
    }
    current.entries = load_entries()
    if #current.entries > 0 then
        osd(string.format("%d mark(s) saved for this file — Alt+L to list", #current.entries), 3)
    end
end

local function add(kind)
    if not current then osd("no local file is playing"); return end
    local t = mp.get_property_number("time-pos")
    if not t then return end
    local rec = {
        v = 1, op = "add", kind = kind, time = t,
        id = string.format("%d-%06d", os.time(), math.random(0, 999999)),
        path = current.path, filename = current.filename, size = current.size,
        created = os.time(),
    }
    if append(rec) then
        table.insert(current.entries, { id = rec.id, kind = kind, time = t, created = rec.created })
        local n = 0
        for _, e in ipairs(current.entries) do if e.kind == kind then n = n + 1 end end
        osd(string.format("%s #%d · %s", KIND_LABEL[kind], n, fmt_time(t)))
    end
end

local function undo()
    if not current or #current.entries == 0 then osd("nothing to undo"); return end
    local last = table.remove(current.entries)
    if append({ v = 1, op = "del", target = last.id, path = current.path, filename = current.filename,
                size = current.size, created = os.time() }) then
        osd(string.format("removed %s at %s", KIND_LABEL[last.kind] or last.kind, fmt_time(last.time)))
    else
        table.insert(current.entries, last)
    end
end

local function list()
    if not current or #current.entries == 0 then osd("no marks for this file"); return end
    local sorted = {}
    for _, e in ipairs(current.entries) do table.insert(sorted, e) end
    table.sort(sorted, function(a, b) return a.time < b.time end)
    local lines = {}
    for i, e in ipairs(sorted) do
        if i > 20 then table.insert(lines, string.format("… and %d more", #sorted - 20)); break end
        table.insert(lines, string.format("%s  %s", fmt_time(e.time), KIND_LABEL[e.kind] or e.kind))
    end
    mp.osd_message("ACS marks:\n" .. table.concat(lines, "\n"), 6)
end

math.randomseed(os.time())
mp.register_event("file-loaded", on_file_loaded)
local function key(k) return (k ~= nil and k ~= "") and k or nil end  -- empty = no default key
mp.add_key_binding(key(opts.key_marker), "acs-marker", function() add("marker") end)
mp.add_key_binding(key(opts.key_clip_start), "acs-clip-start", function() add("in") end)
mp.add_key_binding(key(opts.key_clip_end), "acs-clip-end", function() add("out") end)
mp.add_key_binding(key(opts.key_undo), "acs-undo", undo)
mp.add_key_binding(key(opts.key_list), "acs-list", list)
-- also reachable as "script-message-to acs_markers acs-add marker|in|out" (used by the tests)
mp.register_script_message("acs-add", function(kind) add(kind or "marker") end)
mp.register_script_message("acs-undo", undo)
