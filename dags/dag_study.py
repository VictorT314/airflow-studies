from airflow.sdk import dag, task
from datetime import datetime


@dag(
    dag_id="dag_study",
    schedule=None,
    start_date=datetime(2026, 1, 1),
    catchup=False,
    params={
        "greeting": "Hello",
        "target": "Airflow",
    },
)
def dag_study():

    @task
    def hello_task(**context):
        conf = context["params"]
        greeting = conf.get("greeting")
        target = conf.get("target")
        print(f"{greeting}, {target}!")

    @task
    def goodbye_task(**context):
        conf = context["params"]
        target = conf.get("target")
        print(f"Goodbye, {target}!")

    hello_task() >> goodbye_task()


dag_study()
