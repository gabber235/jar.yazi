--- @since 26.9.1

local M = {}

local LIMITS = {
	max_entries = 100000,
	max_entry_size = 268435456,
	max_path_bytes = 1024,
	max_path_depth = 64,
}

local hovered = ya.sync(function()
	local file = cx.active.current.hovered
	if not file then
		return nil
	end
	return {
		url = file.url,
		is_dir = file.cha.is_dir,
		is_regular = file.url.spec.is_regular,
		ext = file.url.ext,
	}
end)

local function fs_error(message)
	return Error.fs {
		kind = "Other",
		message = message,
	}
end

local function notify(level, message)
	ya.notify {
		title = "jar.yazi",
		content = message,
		level = level,
		timeout = 5,
	}
end

local function is_jar(file)
	return file.is_regular and file.ext and file.ext:lower() == "jar"
end

local function mount(file)
	if not file then
		notify("warn", "No file is hovered")
		return
	end
	if not is_jar(file) then
		notify("warn", "Hovered file is not a local JAR")
		return
	end

	ya.emit("cd", { Url("jar://local/" .. tostring(file.url)) })
end

local function split_fields(line)
	local fields = {}
	for field in (line .. "\t"):gmatch "(.-)\t" do
		fields[#fields + 1] = field
	end
	return fields
end

local function decode(value)
	return value:gsub("%%(%x%x)", function(hex)
		return string.char(tonumber(hex, 16))
	end)
end

local function helper_path(self)
	local plugin = self._id:match "^[^.]+" or self._id
	return tostring(rt.path.config_dir:join("plugins/" .. plugin .. ".yazi/jar_helper.py"))
end

local function cache_path()
	return tostring(rt.path.runtime_dir:join "jar.yazi")
end

local function helper_args(self, command, archive, inner_path)
	return {
		helper_path(self),
		"--cache-dir",
		cache_path(),
		"--max-entries",
		tostring(LIMITS.max_entries),
		"--max-entry-size",
		tostring(LIMITS.max_entry_size),
		"--max-path-bytes",
		tostring(LIMITS.max_path_bytes),
		"--max-path-depth",
		tostring(LIMITS.max_path_depth),
		command,
		archive,
		inner_path,
	}
end

local function run_helper(self, command, archive, inner_path, extra)
	local python = ya.target_family() == "windows" and "python" or "python3"
	local args = helper_args(self, command, archive, inner_path)
	for _, value in ipairs(extra or {}) do
		args[#args + 1] = tostring(value)
	end

	local output, err = Command(python):arg(args):output()
	if not output then
		return nil, fs_error("Cannot start Python 3: " .. tostring(err))
	end
	if not output.status.success then
		local message = output.stderr:gsub("%s+$", "")
		local fields = split_fields(message)
		if #fields >= 2 then
			message = decode(fields[2])
		end
		if message == "" then
			message = "Python helper failed with status " .. tostring(output.status.code)
		end
		return nil, fs_error(message)
	end
	return output.stdout
end

local function archive_parts(url)
	local base = url.base
	if not base then
		return nil, nil, fs_error "JAR mount URL has no archive root"
	end
	local relative = url:strip_prefix(base)
	local inner_path = relative and tostring(relative) or ""
	if inner_path == "." then
		inner_path = ""
	end
	return tostring(base.path), inner_path
end

local function parse_node(line, expected_record)
	local fields = split_fields(line:gsub("%s+$", ""))
	if fields[1] ~= expected_record or #fields ~= 5 then
		return nil, fs_error "Python helper returned invalid metadata"
	end
	local kind, size, mtime = fields[3], tonumber(fields[4]), tonumber(fields[5])
	if (kind ~= "dir" and kind ~= "file") or not size or not mtime then
		return nil, fs_error "Python helper returned invalid node fields"
	end
	return {
		name = decode(fields[2]),
		kind = kind,
		size = size,
		mtime = mtime,
	}
end

local function cha(node)
	return Cha {
		mode = tonumber(node.kind == "dir" and "40555" or "100444", 8),
		len = node.size,
		mtime = node.mtime,
	}
end

local function file(self, job, url)
	local archive, inner_path, parts_err = archive_parts(url)
	if not archive then
		return nil, parts_err
	end
	local output, helper_err = run_helper(self, "stat", archive, inner_path)
	if not output then
		return nil, helper_err
	end
	local node, parse_err = parse_node(output, "NODE")
	if not node then
		return nil, parse_err
	end
	return File {
		url = url,
		cha = cha(node),
	}
end

function M:setup(opts)
	self.open_multi = opts.open_multi or false
end

local function open_inner(self, file)
	local archive, inner_path, parts_err = archive_parts(file.url)
	if not archive then
		notify("error", tostring(parts_err))
		return
	end
	local output, helper_err = run_helper(self, "prepare", archive, inner_path)
	if not output then
		notify("error", tostring(helper_err))
		return
	end
	local fields = split_fields(output:gsub("%s+$", ""))
	if fields[1] ~= "READY" or #fields ~= 2 then
		notify("error", "Python helper returned an invalid prepared path")
		return
	end
	ya.emit("open", { Url(decode(fields[2])) })
end

function M:entry(job)
	local file = hovered()
	local command = job.args[1] or "smart-enter"
	if command == "mount" then
		mount(file)
		return
	end
	if command ~= "smart-enter" then
		notify("error", "Unknown command: " .. command)
		return
	end
	if file and file.is_dir then
		ya.emit("enter", {})
	elseif file and is_jar(file) then
		mount(file)
	elseif file and not file.is_regular then
		open_inner(self, file)
	else
		ya.emit("open", { hovered = not self.open_multi })
	end
end

function M:Capabilities()
	return {}
end

function M:Absolute(job)
	return job.url
end

function M:Canonicalize(job)
	return job.url
end

function M:Casefold(job)
	return job.url
end

function M:Metadata(job)
	local result, err = file(self, job, job.url)
	return result and result.cha, err
end

function M:SymlinkMetadata(job)
	return self:Metadata(job)
end

function M:File(job)
	return file(self, job, job.url)
end

function M:Revalidate(job)
	local latest, err = file(self, job, job.file.url)
	if not latest then
		return nil, err
	end
	local previous = job.file.cha
	if
		latest.cha.is_dir == previous.is_dir
		and latest.cha.len == previous.len
		and latest.cha.mtime == previous.mtime
	then
		return nil
	end
	return latest
end

function M:ReadDir(job)
	local archive, inner_path, parts_err = archive_parts(job.url)
	if not archive then
		return nil, parts_err
	end
	local output, helper_err = run_helper(self, "list", archive, inner_path)
	if not output then
		return nil, helper_err
	end

	local entries = {}
	local first = true
	for line in output:gmatch "[^\r\n]+" do
		if first then
			local header = split_fields(line)
			if header[1] ~= "INDEX" or header[2] ~= "1" then
				return nil, fs_error "Python helper returned an unsupported protocol"
			end
			first = false
		else
			local node, parse_err = parse_node(line, "ENTRY")
			if not node then
				return nil, parse_err
			end
			local metadata = cha(node)
			entries[#entries + 1] = {
				cha = metadata,
				file = File {
					url = job.url:join(node.name),
					cha = metadata,
				},
			}
		end
	end
	if first then
		return nil, fs_error "Python helper returned no directory header"
	end
	return entries
end

function M:Open(job)
	local demand = job.demand
	if demand.append or demand.create or demand.create_new or demand.truncate or demand.write then
		return nil, fs_error "JAR mounts are read only"
	end
	if not demand.read then
		return nil, fs_error "JAR entry was opened without read access"
	end
	local archive, inner_path, parts_err = archive_parts(job.url)
	if not archive then
		return nil, parts_err
	end
	local output, helper_err = run_helper(self, "prepare", archive, inner_path)
	if not output then
		return nil, helper_err
	end
	if not output:match "^READY\t" then
		return nil, fs_error "Python helper did not prepare the JAR entry"
	end
	return 0
end

function M:Read(job)
	local archive, inner_path, parts_err = archive_parts(job.url)
	if not archive then
		return nil, parts_err
	end
	return run_helper(self, "read", archive, inner_path, { job.offset, job.len })
end

function M:provide(job)
	local handler = self[job.op]
	if not handler then
		return nil, fs_error("JAR mounts are read only. Unsupported operation: " .. job.op)
	end
	return handler(self, job)
end

return M
