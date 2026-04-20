import os
import json
import logging
from airflow.models import DagBag
from airflow.sdk import dag, task
from airflow.exceptions import AirflowException
from airflow.providers.standard.operators.trigger_dagrun import (
    TriggerDagRunOperator,
)
from datetime import datetime, timedelta

log = logging.getLogger(__name__)

DAGS_DIR = "/opt/airflow/dags"
INPUT_FILE = os.path.realpath(
    os.path.join(os.path.dirname(__file__), "..", "inputs", "executions.json")
)
MTIME_FILE = os.path.join(os.path.dirname(INPUT_FILE), "inputs_last_mtime")

# Lido no momento do parse para construir o grafo de operadores estaticamente
with open(INPUT_FILE) as _f:
    _config = json.load(_f)


@dag(
    dag_id="dynamic_trigger_control_dag",
    schedule="0 22,2 * * *",
    start_date=datetime(2026, 1, 1),
    catchup=False,
    max_active_runs=1,
    is_paused_upon_creation=False,
    tags=["RUN", "dag-trigger", "control"],
    description=(
        "DAG de controle que monitora alterações no arquivo de inputs "
        "(executions.json) e dispara dinamicamente as DAGs configuradas "
        "para cada ticket. Executa duas vezes ao dia (19h e 23h). "
        "Ao final, remove do arquivo apenas as execuções bem-sucedidas."
    ),
)
def dynamic_trigger_control_dag():

    @task.short_circuit(
        doc_md=(
            "Verifica se o arquivo executions.json foi modificado desde a "
            "última execução e se há execuções pendentes. Caso o arquivo não "
            "tenha mudado ou esteja sem execuções, encerra o pipeline com "
            "sucesso sem disparar as tasks seguintes (short-circuit)."
        )
    )
    def check_input_changes() -> bool:
        """
        Verifica se o arquivo de inputs foi alterado desde a última execução e
        se há execuções pendentes. Encerra com sucesso (sem disparar tasks
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

    @task(
        retries=5,
        retry_delay=timedelta(minutes=2),
        doc_md=(
            "Verifica se todos os dag_ids do ticket estão presentes no "
            "DagBag. Retenta até 5 vezes com intervalo de 2 minutos entre "
            "cada tentativa, aguardando que DAGs recém-adicionadas sejam "
            "carregadas pelo scheduler."
        ),
    )
    def check_dags_in_dagbag(ticket_id: str, dag_ids: list) -> None:
        """
        Verifica se todos os dag_ids do ticket estão presentes no DagBag.
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
                f"[Ticket {ticket_id}] DAGs não encontradas no DagBag: "
                f"{missing}"
            )

        log.info(f"[Ticket {ticket_id}] Todas as DAGs confirmadas: {dag_ids}.")

    @task(
        doc_md=(
            "Registra o sucesso do processamento completo de um ticket e "
            "retorna o ticket_id para a task de limpeza. É ignorada "
            "(skipped) caso qualquer task anterior do mesmo ticket tenha "
            "falhado."
        )
    )
    def report_success(ticket_id: str) -> str:
        """
        Retorna o ticket_id se todo o processamento do chamado foi bem-sucedido.
        É ignorada (skipped) caso qualquer task anterior do chamado tenha
        falhado.
        """
        log.info(f"[Ticket {ticket_id}] Processamento concluído com sucesso.")
        return ticket_id

    @task(
        trigger_rule="all_done",
        doc_md=(
            "Remove do arquivo executions.json somente as execuções cujos "
            "tickets foram processados com sucesso. Execuções com falha são "
            "mantidas para reprocessamento na próxima execução da DAG. "
            "Executada sempre ao final, independentemente do resultado das "
            "tasks anteriores (trigger_rule=all_done)."
        ),
    )
    def cleanup_inputs(successful_tickets: list) -> None:
        """
        Remove do arquivo de inputs apenas as execuções processadas com sucesso.
        Execuções que falharam são mantidas para reprocessamento.
        """
        log.info(f"Limpando execuções bem-sucedidas do arquivo {INPUT_FILE!r}")

        successful = {t for t in successful_tickets if t is not None}
        log.info(
            f"Execuções bem-sucedidas a remover: {successful or 'nenhuma'}"
        )

        with open(INPUT_FILE) as f:
            config = json.load(f)

        original = config.get("executions", [])
        remaining = [e for e in original if e["ticket_id"] not in successful]

        with open(INPUT_FILE, "w") as f:
            json.dump({"executions": remaining}, f, indent=4)

        removed = len(original) - len(remaining)
        log.info(
            f"{removed} execução(ões) removida(s). "
            f"{len(remaining)} execução(ões) mantida(s)."
        )

    file_changed = check_input_changes()
    success_reports = []

    for execution in _config["executions"]:
        ticket_id = execution["ticket_id"]
        dag_entries = execution["dag_ids"]
        dag_ids = [entry["dag_id"] for entry in dag_entries]

        checked = check_dags_in_dagbag.override(
            task_id=f"check_dags__{ticket_id}"
        )(ticket_id=ticket_id, dag_ids=dag_ids)

        file_changed >> checked

        prev = checked
        for entry in dag_entries:
            dag_id = entry["dag_id"]
            conf = entry.get("inputs", {})

            trigger_op = TriggerDagRunOperator(
                task_id=f"trigger__{ticket_id}__{dag_id}",
                trigger_dag_id=dag_id,
                trigger_run_id=(
                    f"ticket__{ticket_id}__{{{{ ts_nodash }}}}__{dag_id}"
                ),
                conf=conf,
                wait_for_completion=True,
                poke_interval=30,
                execution_timeout=timedelta(hours=1),
                allowed_states=["success"],
                failed_states=["failed"],
            )
            prev >> trigger_op
            prev = trigger_op

        success_op = report_success.override(
            task_id=f"report_success__{ticket_id}"
        )(ticket_id=ticket_id)
        prev >> success_op
        success_reports.append(success_op)

    cleanup_inputs(success_reports)


dynamic_trigger_control_dag()
