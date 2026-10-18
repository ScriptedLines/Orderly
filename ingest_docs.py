import glob
from pathlib import Path
from services import _store_chunks


def main():
    docs_dir = Path("docs")
    if not docs_dir.exists():
        print("Error: 'docs' directory not found.")
        return

    supported_patterns = ["docs/*.md", "docs/*.pdf", "docs/*.xlsx", "docs/*.csv"]
    all_files = []
    for pat in supported_patterns:
        all_files.extend(glob.glob(pat))

    if not all_files:
        print("No documents found in docs/ matching supported formats (.md, .pdf, .xlsx, .csv).")
        return

    print("=" * 60)
    print(f"Ingesting {len(all_files)} policy documents into Supabase Vector Store:")
    for f in all_files:
        print(f" - {f}")
    print("=" * 60)

    total_chunks = _store_chunks(all_files)
    print(f"\nSuccessfully stored {total_chunks} chunks in vector store.")


if __name__ == "__main__":
    main()

