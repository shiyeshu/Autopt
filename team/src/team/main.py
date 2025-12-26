import sys
import os
from pathlib import Path
from dotenv import load_dotenv

# 路径修复
current_file = Path(__file__).resolve()
project_root = current_file.parent.parent.parent
env_path = project_root / ".env"
load_dotenv(dotenv_path=env_path)
sys.path.append(str(project_root))

from src.team.crew import RedTeamCrew

def run():
    print("### CLI 模式启动 ###")
    target = input("Target IP: ")
    max_rounds = int(input("Max Rounds: "))
    
    # 清空日志
    log_file = project_root / "mission.log"
    if log_file.exists():
        os.remove(log_file)

    inputs = {'target': target, 'previous_result': "Init."}
    red_team = RedTeamCrew()

    for i in range(1, max_rounds + 1):
        print(f"\n>>> Round {i} <<<")
        assault = red_team.assault_crew()
        result = assault.kickoff(inputs=inputs)
        inputs['previous_result'] = str(result)
        if "MISSION COMPLETE" in str(result): break

    print("\nGenerating Report...")
    with open(log_file, "r", encoding="utf-8") as f:
        log_content = f.read()
    
    reporter = red_team.reporting_crew()
    report = reporter.kickoff(inputs={"log_content": log_content})
    
    print("\n=== REPORT ===\n")
    print(report)

if __name__ == "__main__":
    run()