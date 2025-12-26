import os
from crewai import Agent, Crew, Process, Task, LLM
from crewai.project import CrewBase, agent, task
#from .tools.system_tools import SystemTools
from .tools.custom_tools import ExecutionTool, ListCustomTool
from .tools.system_tools import file_read_tool, file_write_tool

@CrewBase
class RedTeamCrew:
    agents_config = 'config/agents.yaml'
    tasks_config = 'config/tasks.yaml'


    # 这里建议把 Temperature 调低，减少编造概率
    def _create_llm(self, role, temp=0.1 ,**kwargs):
        return LLM(
            model=os.environ.get(f"{role}_MODEL", "openai/gpt-4o"), 
            temperature=temp,
            **kwargs

        )

    # --- Agents (严格对应 YAML) ---

    @agent
    def strategist(self) -> Agent:
        return Agent(config=self.agents_config['strategist'], verbose=True, llm=self._create_llm("STRATEGIST", 0.1),tools=[file_read_tool])

    @agent
    def deputy(self) -> Agent:
        return Agent(config=self.agents_config['deputy'], verbose=True, llm=self._create_llm("DEPUTY", 0.1))

    @agent
    def operator(self) -> Agent:
        return Agent(
            config=self.agents_config['operator'], 
            verbose=True, 
            tools=[ListCustomTool()], 
            llm=self._create_llm("OPERATOR", 0.1) # 低温，防瞎编路径
        )

    @agent
    def auditor(self) -> Agent:
        return Agent(
            config=self.agents_config['auditor'], 
            verbose=True,
            max_iter=5,
            tools=[ExecutionTool(),file_write_tool], 
            llm=self._create_llm("AUDITOR", 0.1,stop=["Observation:", "\nObservation:"]) # 0温度，绝对客观
        )

    @agent
    def logger(self) -> Agent:
        return Agent(
            config=self.agents_config['logger'], 
            verbose=True, 
            tools=[file_write_tool],
            llm=self._create_llm("LOGGER", 0.1)
        )




    @agent
    def reporter(self) -> Agent:
        return Agent(
            config=self.agents_config['reporter'],
            verbose=True,
            # 关键修改：赋予 Reporter 读取日志的工具
            tools=[file_read_tool, file_write_tool], 
            llm=self._create_llm("REPORTER", 0.1)
        )

    # --- Tasks ---

    @task
    def strategy_task(self) -> Task:
        return Task(config=self.tasks_config['strategy_task'],agent=self.strategist())

    @task
    def deputy_task(self) -> Task:
        return Task(config=self.tasks_config['deputy_task'], context=[self.strategy_task()], agent=self.deputy())

    @task
    def operator_task(self) -> Task:
        return Task(config=self.tasks_config['operator_task'], context=[self.deputy_task()], agent=self.operator())

    @task
    def auditor_task(self) -> Task:
        return Task(config=self.tasks_config['auditor_task'], context=[self.operator_task()], agent=self.auditor())

    @task
    def logger_task(self) -> Task:
        # 关键：Logger 必须依赖 Auditor 的输出
        return Task(config=self.tasks_config['logger_task'],context=[self.auditor_task()] , agent=self.logger())

    @task
    def reporting_task(self) -> Task:
        return Task(config=self.tasks_config['reporting_task'], agent=self.reporter(),verbose=True)

    # --- Crews ---

    def assault_crew(self) -> Crew:
        return Crew(
            agents=[self.strategist(), self.deputy(), self.operator(), self.auditor(), self.logger()],
            tasks=[self.strategy_task(), self.deputy_task(), self.operator_task(), self.auditor_task(), self.logger_task()],
            process=Process.sequential
        )

    def reporting_crew(self) -> Crew:
        return Crew(agents=[self.reporter()], tasks=[self.reporting_task()], verbose=True)