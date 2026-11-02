r"""合成大图：生成一个**纯虚构**的大记忆库，用来测规模（默认约 3000 节点）。

不读任何真实资料；生成的内容是随机拼的，只用于压测与演示成长动画。

用法：
  python examples/generate_synthetic.py --out D:\tmp\synthetic --entries 1200
  # 然后用 she-memory init 建配置，把 memory_root 指向 <out>/library，再 build
"""
import argparse
import json
import random
from pathlib import Path

OBJECTS = ["温室", "阀门", "控制器", "传感器", "水泵", "风机", "照明", "配电箱",
           "记录仪", "网关", "滤网", "消毒灯"]
TOPICS = ["巡检", "校准", "更换", "排障", "升级", "清洁", "复盘", "复盘记录"]
WORDS = ["读数", "压力", "流量", "异常", "复位", "日志", "备件", "温漂", "告警"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    parser.add_argument("--entries", type=int, default=1200)
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()
    rng = random.Random(args.seed)

    out = Path(args.out).resolve()
    lib = out / "library"
    for sub in ("entries", "knowledge", "experience", "links"):
        (lib / sub).mkdir(parents=True, exist_ok=True)

    entities = [{"id": f"obj{i}", "name": name, "kind": "object", "aliases": [name]}
                for i, name in enumerate(OBJECTS)]
    (out / "entities.json").write_text(json.dumps(entities, ensure_ascii=False, indent=1), encoding="utf-8")

    (lib / "open-threads.md").write_text(
        "# 待办\n\n| 事项 | 状态 |\n| --- | --- |\n| 合成库巡检 | 进行中 |\n", encoding="utf-8")
    (lib / "promises.md").write_text(
        "# 承诺\n\n| 承诺 | 对象 |\n| --- | --- |\n| 合成库复盘 | 温室 |\n", encoding="utf-8")
    (lib / "profile.md").write_text("# 合成库\n\n这是生成的合成资料，不对应任何真实对象。\n", encoding="utf-8")

    for i in range(args.entries):
        obj = rng.choice(OBJECTS)
        topic = rng.choice(TOPICS)
        word = rng.choice(WORDS)
        body = "\n".join(
            f"{rng.choice(['上午', '下午', '夜里'])}检查{obj}，{word}正常。"
            for _ in range(rng.randint(2, 5)))
        text = f"# 合成-{i:04d} {obj}{topic}\n\n{body}\n\n- 步骤：关阀、读数、回读日志\n"
        (lib / "entries" / f"synthetic-{i:04d}.md").write_text(text, encoding="utf-8")

    for i in range(60):
        obj = rng.choice(OBJECTS)
        (lib / "knowledge" / f"synthetic-knowledge-{i:02d}.md").write_text(
            f"# {obj} 知识 {i}\n\n{obj} 的检查要点：读数、压力、回读日志。\n", encoding="utf-8")
    for i in range(30):
        obj = rng.choice(OBJECTS)
        (lib / "experience" / f"synthetic-experience-{i:02d}.md").write_text(
            f"# {obj} 经验 {i}\n\n排查顺序：读数 → 压力 → 复位 → 回读日志。\n", encoding="utf-8")

    facts = {f"obj{i}": {"model": f"X-{i:02d}", "count": rng.randint(1, 9)} for i in range(len(OBJECTS))}
    (lib / "01_synthetic.yaml").write_text(
        "\n".join(f"{k}:\n  model: {v['model']}\n  count: {v['count']}" for k, v in facts.items()) + "\n",
        encoding="utf-8")

    (lib / "links" / "relations.yaml").write_text("relations: []\n", encoding="utf-8")
    print(json.dumps({"out": str(out), "entries": args.entries,
                      "objects": len(OBJECTS), "entities": str(out / 'entities.json')}, ensure_ascii=False))


if __name__ == "__main__":
    main()
