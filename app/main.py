import sys
import os
import zlib
import hashlib


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
    else:
        raise RuntimeError(f"Unknown command #{command}")


if __name__ == "__main__":
    main()
