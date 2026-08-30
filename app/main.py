import sys
import os
import zlib
import hashlib
import ssl
import urllib.request
import urllib.parse


def http_request(url, data=None, headers=None):
    """Make an HTTP request, falling back to unverified SSL if cert verification fails."""
    req_headers = {
        "User-Agent": "git/2.0",
        "Accept": "*/*",
    }
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, data=data, headers=req_headers)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.read()
    except urllib.error.URLError as e:
        # Retry with unverified SSL context (handles environments without CA certs)
        if isinstance(e.reason, ssl.SSLCertVerificationError):
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            with urllib.request.urlopen(req, context=ctx) as resp:
                return resp.read()
        raise


def write_object(data):
    """Write a git object to .git/objects and return its SHA-1 hash."""
    object_hash = hashlib.sha1(data).hexdigest()
    object_dir = os.path.join(".git", "objects", object_hash[:2])
    os.makedirs(object_dir, exist_ok=True)
    object_path = os.path.join(object_dir, object_hash[2:])
    with open(object_path, "wb") as f:
        f.write(zlib.compress(data))
    return object_hash


def write_blob(file_path):
    """Create a blob object from a file and return its SHA-1 hash."""
    with open(file_path, "rb") as f:
        content = f.read()
    blob = f"blob {len(content)}\0".encode() + content
    return write_object(blob)


def write_tree(directory):
    """Recursively create a tree object from a directory and return its SHA-1 hash."""
    entries = []
    for name in os.listdir(directory):
        if name == ".git":
            continue
        full_path = os.path.join(directory, name)
        if os.path.isdir(full_path):
            mode = "40000"
            sha = write_tree(full_path)
        else:
            mode = "100644"
            sha = write_blob(full_path)
        entries.append((mode, name, sha))

    # Sort entries alphabetically by name
    entries.sort(key=lambda e: e[1])

    # Build the tree data: <mode> <name>\0<20_byte_sha> for each entry
    tree_data = b""
    for mode, name, sha in entries:
        tree_data += f"{mode} {name}\0".encode() + bytes.fromhex(sha)

    tree = f"tree {len(tree_data)}\0".encode() + tree_data
    return write_object(tree)


# --- Git clone implementation ---

def read_pkt_line(data, offset):
    """Read a single pkt-line from data starting at offset. Returns (line, new_offset) or (None, offset) for flush."""
    if offset + 4 > len(data):
        return None, offset
    length = int(data[offset:offset + 4], 16)
    if length == 0:
        return None, offset + 4  # flush-pkt
    line = data[offset + 4:offset + length]
    return line, offset + length


def parse_refs(advertisement):
    """Parse the ref advertisement (pkt-line stream) and return (refs_dict, capabilities)."""
    refs = {}
    capabilities = None
    offset = 0
    # First pkt-line is "# service=git-upload-pack\n", followed by a flush (0000)
    line, offset = read_pkt_line(advertisement, offset)
    # Skip the flush after the service line
    line, offset = read_pkt_line(advertisement, offset)
    while offset < len(advertisement):
        line, offset = read_pkt_line(advertisement, offset)
        if line is None:
            break
        # Parse: <sha> <refname>\0<capabilities>
        parts = line.split(b" ", 1)
        if len(parts) < 2:
            continue
        sha = parts[0].decode()
        rest = parts[1]
        if b"\0" in rest:
            refname, caps = rest.split(b"\0", 1)
            if capabilities is None:
                capabilities = caps.decode()
        else:
            refname = rest
        refname = refname.decode()
        # Strip trailing newline (pkt-lines may include LF)
        refname = refname.rstrip("\n")
        # Skip peeled refs like refs/tags/v1.0^{}
        if refname.endswith("^{}"):
            continue
        refs[refname] = sha
    return refs, capabilities


def read_varint(data, offset):
    """Read a variable-length integer from data at offset. Returns (value, new_offset)."""
    value = 0
    shift = 0
    while True:
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            break
        shift += 7
    return value, offset


def read_ofs_delta_offset(data, offset):
    """Read the offset encoding for ofs-delta. Returns (value, new_offset)."""
    byte = data[offset]
    offset += 1
    value = byte & 0x7F
    while byte & 0x80:
        byte = data[offset]
        offset += 1
        value = ((value + 1) << 7) | (byte & 0x7F)
    return value, offset


def apply_delta(base, delta):
    """Apply a git delta to a base object. Returns the reconstructed object."""
    # Parse source size and target size
    src_size, pos = read_varint(delta, 0)
    tgt_size, pos = read_varint(delta, pos)

    result = bytearray()
    while pos < len(delta):
        opcode = delta[pos]
        pos += 1
        if opcode & 0x80:
            # Copy from base
            copy_offset = 0
            copy_size = 0
            if opcode & 0x01:
                copy_offset |= delta[pos]
                pos += 1
            if opcode & 0x02:
                copy_offset |= delta[pos] << 8
                pos += 1
            if opcode & 0x04:
                copy_offset |= delta[pos] << 16
                pos += 1
            if opcode & 0x08:
                copy_offset |= delta[pos] << 24
                pos += 1
            if opcode & 0x10:
                copy_size |= delta[pos]
                pos += 1
            if opcode & 0x20:
                copy_size |= delta[pos] << 8
                pos += 1
            if opcode & 0x40:
                copy_size |= delta[pos] << 16
                pos += 1
            if copy_size == 0:
                copy_size = 0x10000
            result.extend(base[copy_offset:copy_offset + copy_size])
        elif opcode:
            # Insert new data
            result.extend(delta[pos:pos + opcode])
            pos += opcode
        else:
            raise RuntimeError("Invalid delta opcode 0")
    return bytes(result)


def parse_packfile(pack_data):
    """Parse a packfile and return a dict of {sha_hex: (type, data)} for all objects."""
    # Verify PACK signature
    if pack_data[:4] != b"PACK":
        raise RuntimeError("Invalid pack signature")
    version = int.from_bytes(pack_data[4:8], "big")
    num_objects = int.from_bytes(pack_data[8:12], "big")

    # We'll parse objects sequentially. Track offsets for ofs-delta.
    # Store raw objects first, then resolve deltas.
    objects = []  # list of (type, data_or_delta_info, offset)
    offset = 12
    for _ in range(num_objects):
        obj_offset = offset
        # Parse object header
        byte = pack_data[offset]
        offset += 1
        obj_type = (byte >> 4) & 0x07
        size = byte & 0x0F
        shift = 4
        while byte & 0x80:
            byte = pack_data[offset]
            offset += 1
            size |= (byte & 0x7F) << shift
            shift += 7

        if obj_type == 6:  # OBJ_OFS_DELTA
            neg_offset, offset = read_ofs_delta_offset(pack_data, offset)
            base_offset = obj_offset - neg_offset
            # Decompress delta data
            decompressor = zlib.decompressobj()
            delta_data = decompressor.decompress(pack_data[offset:])
            offset += len(pack_data[offset:]) - len(decompressor.unused_data)
            objects.append((obj_type, (base_offset, delta_data), obj_offset))
        elif obj_type == 7:  # OBJ_REF_DELTA
            base_sha = pack_data[offset:offset + 20].hex()
            offset += 20
            decompressor = zlib.decompressobj()
            delta_data = decompressor.decompress(pack_data[offset:])
            offset += len(pack_data[offset:]) - len(decompressor.unused_data)
            objects.append((obj_type, (base_sha, delta_data), obj_offset))
        else:
            # Non-delta object: decompress
            decompressor = zlib.decompressobj()
            obj_data = decompressor.decompress(pack_data[offset:])
            offset += len(pack_data[offset:]) - len(decompressor.unused_data)
            objects.append((obj_type, obj_data, obj_offset))

    # Resolve objects: build a map from offset to (type, data)
    resolved_by_offset = {}
    resolved_by_sha = {}

    # First pass: resolve non-delta objects
    for obj_type, data, obj_offset in objects:
        if obj_type in (1, 2, 3, 4):  # commit, tree, blob, tag
            type_names = {1: "commit", 2: "tree", 3: "blob", 4: "tag"}
            type_name = type_names[obj_type]
            full = f"{type_name} {len(data)}\0".encode() + data
            sha = hashlib.sha1(full).hexdigest()
            resolved_by_offset[obj_offset] = (type_name, data)
            resolved_by_sha[sha] = (type_name, data)

    # Resolve deltas (may need multiple passes for chained deltas)
    remaining = [(obj_type, info, obj_offset) for obj_type, info, obj_offset in objects
                 if obj_type in (6, 7)]
    while remaining:
        progress = False
        still_remaining = []
        for obj_type, info, obj_offset in remaining:
            if obj_type == 6:  # ofs-delta
                base_offset, delta_data = info
                if base_offset in resolved_by_offset:
                    base_type, base_data = resolved_by_offset[base_offset]
                    result = apply_delta(base_data, delta_data)
                    full = f"{base_type} {len(result)}\0".encode() + result
                    sha = hashlib.sha1(full).hexdigest()
                    resolved_by_offset[obj_offset] = (base_type, result)
                    resolved_by_sha[sha] = (base_type, result)
                    progress = True
                else:
                    still_remaining.append((obj_type, info, obj_offset))
            else:  # ref-delta
                base_sha, delta_data = info
                if base_sha in resolved_by_sha:
                    base_type, base_data = resolved_by_sha[base_sha]
                    result = apply_delta(base_data, delta_data)
                    full = f"{base_type} {len(result)}\0".encode() + result
                    sha = hashlib.sha1(full).hexdigest()
                    resolved_by_offset[obj_offset] = (base_type, result)
                    resolved_by_sha[sha] = (base_type, result)
                    progress = True
                else:
                    still_remaining.append((obj_type, info, obj_offset))
        if not progress:
            raise RuntimeError("Could not resolve all deltas")
        remaining = still_remaining

    return resolved_by_sha


def write_loose_objects(objects, git_dir):
    """Write all objects (dict of sha -> (type, data)) as loose objects."""
    for sha, (obj_type, data) in objects.items():
        full = f"{obj_type} {len(data)}\0".encode() + data
        object_dir = os.path.join(git_dir, "objects", sha[:2])
        os.makedirs(object_dir, exist_ok=True)
        object_path = os.path.join(object_dir, sha[2:])
        with open(object_path, "wb") as f:
            f.write(zlib.compress(full))


def read_object(sha, git_dir=".git"):
    """Read a loose git object by SHA. Returns (type, data)."""
    object_path = os.path.join(git_dir, "objects", sha[:2], sha[2:])
    with open(object_path, "rb") as f:
        compressed = f.read()
    decompressed = zlib.decompress(compressed)
    header, data = decompressed.split(b"\0", 1)
    obj_type = header.split(b" ")[0].decode()
    return obj_type, data


def checkout_tree(tree_sha, path, git_dir=".git"):
    """Recursively checkout a tree object into the given directory path."""
    obj_type, data = read_object(tree_sha, git_dir)
    if obj_type != "tree":
        raise RuntimeError(f"Expected tree, got {obj_type}")

    # Parse tree entries
    entries = []
    i = 0
    while i < len(data):
        space_idx = data.index(b" ", i)
        mode = data[i:space_idx].decode()
        null_idx = data.index(b"\0", space_idx)
        name = data[space_idx + 1:null_idx].decode()
        sha = data[null_idx + 1:null_idx + 21].hex()
        entries.append((mode, name, sha))
        i = null_idx + 21

    for mode, name, sha in entries:
        if mode == "40000":
            # Directory
            subdir = os.path.join(path, name)
            os.makedirs(subdir, exist_ok=True)
            checkout_tree(sha, subdir, git_dir)
        elif mode == "160000":
            # Submodule (gitlink) - skip, the object is in another repo
            continue
        else:
            # File
            obj_type, content = read_object(sha, git_dir)
            if obj_type != "blob":
                raise RuntimeError(f"Expected blob, got {obj_type}")
            file_path = os.path.join(path, name)
            with open(file_path, "wb") as f:
                f.write(content)


def clone_repo(url, dest_dir):
    """Clone a git repository from a URL into dest_dir."""
    # Strip trailing slash from URL
    url = url.rstrip("/")

    # Step 1: Discover refs
    refs_url = f"{url}/info/refs?service=git-upload-pack"
    advertisement = http_request(refs_url)

    refs, capabilities = parse_refs(advertisement)
    if "HEAD" not in refs:
        raise RuntimeError("No HEAD ref found")
    head_sha = refs["HEAD"]

    # Step 2: Request the packfile
    # Build the request body: want <sha> <capabilities>\n, then done
    want_line = f"want {head_sha} side-band-64k ofs-delta\n"
    body = f"{len(want_line) + 4:04x}{want_line}".encode()
    body += b"0000"  # flush
    done_line = "done\n"
    body += f"{len(done_line) + 4:04x}{done_line}".encode()

    upload_url = f"{url}/git-upload-pack"
    response = http_request(upload_url, data=body, headers={
        "Content-Type": "application/x-git-upload-pack-request",
    })

    # Step 3: Parse the response (side-band-64k multiplexed)
    # The response is a series of pkt-lines. Sideband 1 contains packfile data.
    pack_data = b""
    offset = 0
    while offset < len(response):
        line, offset = read_pkt_line(response, offset)
        if line is None:
            break
        if line and line[0] == 1:  # sideband 1: pack data
            pack_data += line[1:]
        elif line and line[0] == 2:  # sideband 2: progress (ignore)
            pass
        elif line and line[0] == 3:  # sideband 3: error
            raise RuntimeError(f"Server error: {line[1:].decode()}")

    # Step 4: Parse the packfile and write loose objects
    objects = parse_packfile(pack_data)
    git_dir = os.path.join(dest_dir, ".git")
    write_loose_objects(objects, git_dir)

    # Step 5: Set up .git directory structure
    os.makedirs(os.path.join(git_dir, "objects"), exist_ok=True)
    os.makedirs(os.path.join(git_dir, "refs", "heads"), exist_ok=True)
    os.makedirs(os.path.join(git_dir, "refs", "tags"), exist_ok=True)

    # Write HEAD - point to the default branch (from symref capability if available)
    head_ref = "refs/heads/main"
    if capabilities:
        for cap in capabilities.split():
            if cap.startswith("symref=HEAD:"):
                head_ref = cap.split(":", 1)[1]
                break
    with open(os.path.join(git_dir, "HEAD"), "w") as f:
        f.write(f"ref: {head_ref}\n")

    # Write refs (only those pointing to objects we have)
    for refname, sha in refs.items():
        if refname == "HEAD":
            continue
        if sha not in objects:
            continue
        ref_path = os.path.join(git_dir, refname)
        os.makedirs(os.path.dirname(ref_path), exist_ok=True)
        with open(ref_path, "w") as f:
            f.write(sha + "\n")

    # Step 6: Checkout the working tree from HEAD commit
    obj_type, commit_data = read_object(head_sha, git_dir)
    if obj_type != "commit":
        raise RuntimeError(f"HEAD is not a commit, got {obj_type}")
    # Parse commit to find tree sha
    tree_sha = None
    for line in commit_data.decode().split("\n"):
        if line.startswith("tree "):
            tree_sha = line.split(" ")[1]
            break
    if tree_sha is None:
        raise RuntimeError("Commit has no tree")
    checkout_tree(tree_sha, dest_dir, git_dir)


def main():
    # You can use print statements as follows for debugging, they'll be visible when running tests.
    print("Logs from your program will appear here!", file=sys.stderr)

    command = sys.argv[1]
    if command == "init":
        os.mkdir(".git")
        os.mkdir(".git/objects")
        os.mkdir(".git/refs")
        with open(".git/HEAD", "w") as f:
            f.write("ref: refs/heads/main\n")
        print("Initialized git directory")
    elif command == "clone":
        url = sys.argv[2]
        dest_dir = sys.argv[3]
        os.makedirs(dest_dir, exist_ok=True)
        clone_repo(url, dest_dir)
    elif command == "cat-file":
        flag = sys.argv[2]
        object_hash = sys.argv[3]
        if flag == "-p":
            # Path to the object file: .git/objects/<first 2 chars>/<remaining 38 chars>
            object_path = os.path.join(".git", "objects", object_hash[:2], object_hash[2:])
            with open(object_path, "rb") as f:
                compressed = f.read()
            decompressed = zlib.decompress(compressed)
            # Format: blob <size>\0<content>
            # Split on the first null byte to separate header from content
            _, content = decompressed.split(b"\0", 1)
            sys.stdout.buffer.write(content)
    elif command == "hash-object":
        flag = sys.argv[2]
        file_path = sys.argv[3]
        with open(file_path, "rb") as f:
            content = f.read()
        # Build the blob object: blob <size>\0<content>
        header = f"blob {len(content)}\0".encode()
        blob = header + content
        # Compute SHA-1 hash over the uncompressed blob
        object_hash = hashlib.sha1(blob).hexdigest()
        if flag == "-w":
            # Write the compressed object to .git/objects/<first 2 chars>/<remaining 38 chars>
            object_dir = os.path.join(".git", "objects", object_hash[:2])
            os.makedirs(object_dir, exist_ok=True)
            object_path = os.path.join(object_dir, object_hash[2:])
            with open(object_path, "wb") as f:
                f.write(zlib.compress(blob))
        print(object_hash)
    elif command == "ls-tree":
        flag = sys.argv[2]
        tree_sha = sys.argv[3]
        # Path to the tree object file
        object_path = os.path.join(".git", "objects", tree_sha[:2], tree_sha[2:])
        with open(object_path, "rb") as f:
            compressed = f.read()
        decompressed = zlib.decompress(compressed)
        # Format: tree <size>\0<entries>
        # Split on the first null byte to separate header from entries
        _, entries_data = decompressed.split(b"\0", 1)

        # Parse entries: each is <mode> <name>\0<20_byte_sha>
        entries = []
        i = 0
        while i < len(entries_data):
            # Find the space separating mode and name
            space_idx = entries_data.index(b" ", i)
            mode = entries_data[i:space_idx].decode()
            # Find the null byte separating name and sha
            null_idx = entries_data.index(b"\0", space_idx)
            name = entries_data[space_idx + 1:null_idx].decode()
            # The 20-byte sha follows the null byte
            sha = entries_data[null_idx + 1:null_idx + 21].hex()
            entries.append((mode, name, sha))
            i = null_idx + 21

        if flag == "--name-only":
            for _, name, _ in entries:
                print(name)
        else:
            for mode, name, sha in entries:
                # Convert mode: 40000 -> 040000, others stay as-is
                if mode == "40000":
                    mode = "040000"
                obj_type = "tree" if mode == "040000" else "blob"
                print(f"{mode} {obj_type} {sha}    {name}")
    elif command == "write-tree":
        tree_sha = write_tree(".")
        print(tree_sha)
    elif command == "commit-tree":
        tree_sha = sys.argv[2]
        # Parse flags: -p <parent_sha> and -m <message>
        parent_sha = None
        message = None
        i = 3
        while i < len(sys.argv):
            if sys.argv[i] == "-p":
                parent_sha = sys.argv[i + 1]
                i += 2
            elif sys.argv[i] == "-m":
                message = sys.argv[i + 1]
                i += 2
            else:
                i += 1

        # Build the commit object content
        author = "John Doe <john@example.com> 1234567890 +0000"
        content = f"tree {tree_sha}\n"
        if parent_sha:
            content += f"parent {parent_sha}\n"
        content += f"author {author}\n"
        content += f"committer {author}\n"
        content += f"\n{message}\n"

        commit = f"commit {len(content)}\0".encode() + content.encode()
        commit_sha = write_object(commit)
        print(commit_sha)
    else:
        raise RuntimeError(f"Unknown command #{command}")


if __name__ == "__main__":
    main()
