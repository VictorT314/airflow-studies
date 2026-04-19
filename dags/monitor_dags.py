import json
import logging
import os
from datetime import datetime, timedelta

from airflow.exceptions import AirflowException
from airflow.models import DagBag
from airflow.providers.standard.operators.trigger_dagrun import TriggerDagRunOperator
from airflow.sdk import dag, task

log = logging.getLogger(__name__)

DAGS_DIR = "/opt/airflow/dags"
INPUT_FILE = os.path.realpath(
    os.path.join(os.path.dirname(__file__), "..", "inputs", "executions.json")
)
MTIME_FILE = os.path.join(os.path.dirname(INPUT_FILE), "monitor_dags_last_mtime")

# Lido no momento do parse para construir o grafo de operadores estaticamente
with open(INPUT_FILE) as _f:
    _config = json.load(_f)


@dag(
    dag_id="monitor_dags",
    schedule="0 19,23 * * *",
    start_date=datetime(2026, 1, 1),
    catchup=False,
)
def monitor_dags():

    @task.short_circuit
    def check_file_changes() -> bool:
        """Verifica se o arquivo de inputs foi alterado desde a última execução
        e se há execuções pendentes. Encerra com sucesso (sem disparar tasks
        seguintes) caso o arquivo não tenha mudado ou esteja sem execuções.
        """
        log.info(f"Verificando alterações no arquivo de inputs {INPUT_FILE!r}")

        try:
            current_mtime = os.stat(INPUT_FILE).st_mtime
        except OSError as exc:
            log.warning(f"Não foi possível acessar o arquivo de inputs: {exc}")
            return False

        mtime_iso = datetime.fromtimestamp(current_mtime).isoformat()
        log.info(f"Mtime atual do arquivo de inputs: {mtime_iso}")

        previous_mtime = 0.0
        if os.path.exists(MTIME_FILE):
            with open(MTIME_FILE) as f:
                try:
                    previous_mtime = float(f.read().strip())
                except ValueError:
                    previous_mtime = 0.0
        previous_mtime_iso = (
            datetime.fromtimestamp(previous_mtime).isoformat()
            if previous_mtime
            else "N/A"
        )
        log.info(f"Mtime anterior registrado: {previous_mtime_iso}")

        if current_mtime <= previous_mtime:
            log.info(
                "Nenhuma alteração detectada no arquivo de inputs. "
                "Encerrando execução com sucesso."
            )
            return False

        with open(INPUT_FILE) as f:
            config = json.load(f)

        executions = config.get("executions", [])
        if not executions:
            log.info(
                "Arquivo de inputs alterado, mas sem execuções pendentes. "
                "Encerrando execução com sucesso."
            )
            with open(MTIME_FILE, "w") as f:
                f.write(str(current_mtime))
            return False

        with open(MTIME_FILE, "w") as f:
            f.write(str(current_mtime))

        log.info(
            f"Alteração detectada em {INPUT_FILE!r} (mtime={mtime_iso}). "
            f"{len(executions)} execução(ões) pendente(s). Prosseguindo."
        )
        return True

    @task(retries=5, retry_delay=timedelta(minutes=2))
    def check_dags_in_dagbag(ticket_id: str, dag_ids: list) -> None:
        """Verifica se todos os dag_ids do ticket estão presentes no DagBag.
        Retenta até 5 vezes com intervalo de 2 minutos se algum estiver ausente.
        """
        log.info(f"[Ticket {ticket_id}] Verificando DAGs no DagBag: {dag_ids}")

        dagbag = DagBag(dag_folder=DAGS_DIR, include_examples=False)
        if dagbag.import_errors:
            log.warning(
                f"[Ticket {ticket_id}] Erros de importação no DagBag: "
                f"{dagbag.import_errors}"
            )

        missing = [d for d in dag_ids if d not in dagbag.dags]
        if missing:
            log.warning(
                f"[Ticket {ticket_id}] DAGs ausentes: {missing}. "
                "Uma nova tentativa será realizada em 2 minutos."
            )
            raise AirflowException(
                f"[Ticket {ticket_id}] DAGs não encontradas no DagBag: {missing}"
            )

        log.info(f"[Ticket {ticket_id}] Todas as DAGs confirmadas: {dag_ids}.")

    @task(trigger_rule="all_done")
    def cleanup_inputs() -> None:
        """Limpa as execuções do arquivo de inputs após o processamento de
        todos os tickets (independentemente de sucesso ou falha).
        """
        log.info(f"Limpando execuções do arquivo de inputs {INPUT_FILE!r}")
        with open(INPUT_FILE, "w") as f:
            json.dump({"executions": []}, f, indent=4)
        log.info("Arquivo de inputs limpo com sucesso.")

    file_changed = check_file_changes()
    cleanup = cleanup_inputs()

    last_ops = []
    for execution in _config["executions"]:
        ticket_id = execution["ticket_id"]
        dag_entries = execution["dag_ids"]
        dag_ids = [entry["dag_id"] for entry in dag_entries]

        checked = check_dags_in_dagbag.override(task_id=f"check_dags__{ticket_id}")(
            ticket_id=ticket_id, dag_ids=dag_ids
        )

        file_changed >> checked

        prev = checked
        for entry in dag_entries:
            dag_id = entry["dag_id"]
            conf = entry.get("conf", {})

            trigger_op = TriggerDagRunOperator(
                task_id=f"trigger__{ticket_id}__{dag_id}",
                trigger_dag_id=dag_id,
                trigger_run_id=f"ticket__{ticket_id}__{{{{ ts_nodash }}}}__{dag_id}",
                conf=conf,
                wait_for_completion=True,
                poke_interval=30,
                execution_timeout=timedelta(hours=1),
                allowed_states=["success"],
                failed_states=["failed"],
            )
            prev >> trigger_op
            prev = trigger_op

        last_ops.append(prev)

    last_ops >> cleanup


monitor_dags()
