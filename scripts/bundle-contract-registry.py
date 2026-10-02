"""Copy the locked canonical operation registry into the standalone Kit."""
import shutil
from pathlib import Path

root = Path(__file__).resolve().parents[1]
source = root.parents[1] / "packages" / "commerce-contracts" / "registry"
generated_source = root.parents[1] / "packages" / "commerce-contracts" / "generated" / "ts" / "index.ts"
destination = root / "runtime" / "contracts" / "registry"

if destination.exists():
    shutil.rmtree(destination)
destination.mkdir(parents=True)
shutil.copy2(source / "registry.json", destination / "registry.json")
shutil.copytree(source / "operations", destination / "operations")
shutil.copy2(generated_source, destination.parent / "generated.ts")
print(destination)

for name in ("schemas", "vectors"):
    shutil.copytree(source.parent / name, destination.parent / name, dirs_exist_ok=True)
