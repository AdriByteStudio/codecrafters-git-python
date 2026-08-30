import sys
import os
import zlib


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
    else:
        raise RuntimeError(f"Unknown command #{command}")


if __name__ == "__main__":
    main()
