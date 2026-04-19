# Airflow — Atendimento de Chamados

Repositório para orquestração de chamados via Apache Airflow. A DAG `monitor_dags` é responsável por detectar alterações no arquivo de inputs, verificar a disponibilidade das DAGs configuradas, executá-las conforme definido e limpar o arquivo ao final.

---

## Estrutura do repositório

```
.
├── dags/                        # DAGs do Airflow
│   ├── monitor_dags.py          # DAG orquestradora principal
│   └── dag_study.py             # Exemplo de DAG de atendimento
├── inputs/
│   └── executions.json          # Arquivo de configuração de execuções
├── config/
│   └── airflow.cfg              # Configuração do Airflow
├── plugins/                     # Plugins customizados (se houver)
├── logs/                        # Logs de execução (não versionado)
├── docker-compose.yaml
└── .env                         # Variáveis de ambiente (não versionado)
```

---

## Pré-requisitos

- Docker e Docker Compose instalados
- Arquivo `.env` configurado na raiz (variáveis: `FERNET_KEY`, `AIRFLOW__API_AUTH__JWT_SECRET`, etc.)

Para subir o ambiente:

```bash
docker compose up -d
```

Acesse o Airflow em `http://localhost:8080` (usuário e senha padrão: `airflow` / `airflow`).

---

## Para o atendente de chamados

O atendente é responsável por registrar quais DAGs devem ser executadas para atender a um chamado, preenchendo o arquivo `inputs/executions.json`.

### Formato do arquivo de inputs

```json
{
    "executions": [
        {
            "ticket_id": "ATD-001",
            "dag_ids": [
                {
                    "dag_id": "nome_da_dag",
                    "conf": {
                        "parametro_1": "valor_1",
                        "parametro_2": "valor_2"
                    }
                }
            ]
        }
    ]
}
```

| Campo | Descrição |
|---|---|
| `ticket_id` | Identificador único do chamado (ex: `ATD-001`) |
| `dag_ids` | Lista ordenada de DAGs a serem executadas para o chamado |
| `dag_id` | Nome da DAG cadastrada no Airflow |
| `conf` | Parâmetros de entrada para a DAG (consulte o desenvolvedor responsável pela DAG) |

### Regras importantes

- Cada entrada em `executions` representa um chamado independente. Chamados diferentes rodam **em paralelo**.
- As DAGs dentro de um mesmo chamado rodam **em sequência**, na ordem em que estão listadas.
- Se uma DAG da sequência falhar, as seguintes **não serão executadas**.
- Um chamado só é disparado se **todas** as suas DAGs estiverem disponíveis no Airflow. Caso contrário, a verificação retenta automaticamente até 5 vezes (com intervalo de 2 minutos) antes de falhar.
- Ao final do processamento, **o arquivo `executions.json` é limpo automaticamente** (`executions: []`), independentemente de sucesso ou falha dos chamados.

### Como registrar e disparar um chamado

1. Edite o arquivo `inputs/executions.json` adicionando a entrada correspondente ao chamado.
2. Salve o arquivo — a alteração será detectada automaticamente na próxima execução da `monitor_dags` (agendada para 19h e 23h).
3. Para disparar imediatamente, acesse o Airflow em `http://localhost:8080`, localize a DAG `monitor_dags` e execute-a manualmente.

> **Atenção:** não edite o arquivo `inputs/executions.json` enquanto a `monitor_dags` estiver em execução. O arquivo é limpo ao final de cada ciclo.

---

## Para o desenvolvedor de DAGs

O desenvolvedor é responsável por criar e manter as DAGs que viabilizam o atendimento dos chamados.

### Criando uma nova DAG de atendimento

1. Crie um novo arquivo `.py` em `dags/`.
2. Use o decorator `@dag` com `schedule=None` (execução apenas via trigger) e `catchup=False`.
3. Declare os parâmetros esperados via `params` no decorator `@dag` — esses valores correspondem ao `conf` definido pelo atendente no arquivo de inputs.
4. Acesse os parâmetros dentro das tasks via `context["params"]`.

**Exemplo mínimo:**

```python
from airflow.sdk import dag, task
from datetime import datetime

@dag(
    dag_id="minha_dag",
    schedule=None,
    start_date=datetime(2026, 1, 1),
    catchup=False,
    params={
        "parametro_1": "valor_padrao",
    },
)
def minha_dag():

    @task
    def processar(**context):
        valor = context["params"]["parametro_1"]
        print(f"Processando com: {valor}")

    processar()

minha_dag()
```

5. Documente os parâmetros aceitos pela DAG para que o atendente saiba como preencher o `conf` no arquivo de inputs.

### Fluxo da DAG `monitor_dags`

```
check_file_changes              → detecta alterações no arquivo inputs/executions.json
                                  encerra sem disparar tasks se o arquivo não mudou
                                  ou se executions estiver vazio
  ├── check_dags__<ticket_A>    → verifica DAGs do ticket A no DagBag (retries: 5, intervalo: 2min)
  │     └── trigger__<ticket_A>__<dag_1>  → dispara e aguarda conclusão
  │           └── trigger__<ticket_A>__<dag_2>  → dispara após dag_1 concluir
  │
  └── check_dags__<ticket_B>    → verifica DAGs do ticket B (em paralelo ao ticket A)
        └── trigger__<ticket_B>__<dag_1>
              │
              └──────────────────────────────────> cleanup_inputs
                                                   (executa ao final de todos os tickets,
                                                    limpa executions.json)
```

- Tickets diferentes são processados **em paralelo**.
- A DAG `monitor_dags` é agendada para rodar às **19h e 23h** diariamente, mas pode ser disparada manualmente a qualquer momento.
- O controle de alterações é baseado no `mtime` do arquivo `inputs/executions.json`, persistido em `inputs/monitor_dags_last_mtime` (não versionado). Esse arquivo sobrevive a reinicializações do container.

### Boas práticas

- Prefira `schedule=None` em DAGs de atendimento — elas devem ser disparadas exclusivamente pela `monitor_dags`.
- Valide os `params` no início da primeira task para falhar rapidamente com mensagem clara caso o `conf` esteja incompleto.
- Mantenha o `dag_id` estável — renomear uma DAG em uso pode quebrar chamados em andamento.

---

## Melhorias futuras

### Confiabilidade e observabilidade

- **Notificações de falha**: integrar callbacks `on_failure_callback` nas tasks críticas (`check_dags_in_dagbag`, `trigger__*`) para envio de alertas via e-mail, Slack ou outro canal, informando o `ticket_id` e o motivo da falha.
- **Registro de histórico de execuções**: persistir em um arquivo ou banco de dados o resultado de cada execução (ticket, DAGs disparadas, status, timestamps), evitando perda de rastreabilidade após a limpeza do `executions.json`.
- **Timeout configurável por ticket**: permitir definir `execution_timeout` individualmente por entrada no `executions.json`, em vez de um valor fixo para todas as DAGs.

### Segurança e integridade

- **Validação do schema do `executions.json`**: validar o arquivo de inputs contra um schema JSON (ex: com `jsonschema`) logo na task `check_file_changes`, rejeitando entradas malformadas antes de iniciar qualquer execução.
- **Controle de duplicidade de `ticket_id`**: detectar e rejeitar entradas com `ticket_id` duplicado no arquivo de inputs, evitando execuções ambíguas.
- **Backup do `executions.json` antes da limpeza**: salvar uma cópia com timestamp do arquivo antes de zerá-lo na task `cleanup_inputs`, preservando o histórico de chamados submetidos.

### Escalabilidade e manutenibilidade

- **Grafo dinâmico via banco de dados**: substituir a leitura do `executions.json` em parse-time por uma fonte de dados persistente (ex: tabela no banco do Airflow ou uma variável do Airflow), eliminando a necessidade de reiniciar o scheduler para que novos chamados sejam refletidos no grafo.
- **Interface de submissão de chamados**: criar um script CLI ou formulário web simples para preencher e validar o `executions.json`, reduzindo erros manuais no preenchimento.
- **Testes automatizados das DAGs**: adicionar testes unitários com `pytest` para validar a estrutura das DAGs (ex: usando `DagBag` para checar ausência de erros de importação) e testes de integração para os fluxos principais.
- **Suporte a múltiplos ambientes**: parametrizar o `docker-compose.yaml` e as DAGs para suportar ambientes distintos (desenvolvimento, homologação, produção) via variáveis de ambiente ou perfis do Docker Compose.
