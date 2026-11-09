"""Import documents through the same service as the HTTP endpoint."""
import argparse
import sys
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.database import create_all, SessionLocal
from app.models import Project
from app.services import ingest_document


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument("--project", default="文档导入项目")
    args = parser.parse_args()
    create_all()
    with SessionLocal.begin() as db:
        project = db.query(Project).filter_by(name=args.project).first()
        if project is None:
            project = Project(name=args.project)
            db.add(project)
            db.flush()
        project_id = project.id
    for path in args.paths:
        print(json.dumps(ingest_document(project_id, path.name, path.read_bytes()), ensure_ascii=False))
    print("project_id=" + project_id)


if __name__ == "__main__":
    main()
