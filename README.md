# IA News

Coletor diário de notícias globais sobre Inteligência Artificial. O script consulta o
[GDELT DOC 2.0](https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/) e, opcionalmente,
feeds RSS configuráveis, e gera um relatório em Markdown com as notícias publicadas no dia
anterior (fuso `America/Sao_Paulo`).

Títulos e descrições são mantidos no idioma original. O script não extrai o texto completo
dos artigos e não contorna paywalls.

## Instalação

Requer Python 3.12.

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Execução

Coleta as notícias do dia anterior:

```bash
python main.py
```

## Reprocessar uma data

```bash
python main.py --date 2026-08-02
```

A data usa o formato `YYYY-MM-DD` e é interpretada no fuso configurado em `config.yaml`.
O GDELT mantém apenas os últimos ~3 meses disponíveis para consulta por intervalo de datas.

## Adicionar feeds RSS

Inclua novas entradas na lista `rss_feeds` do `config.yaml`:

```yaml
rss_feeds:
  - name: Nome do veículo
    url: https://exemplo.com/feed.xml
    enabled: true
```

Feeds com `enabled: false` são ignorados. Itens de RSS só entram no relatório quando o
título ou a descrição casam com os termos da lista `keywords`.

Outros ajustes disponíveis no `config.yaml`:

- `timezone`: fuso usado para definir o dia e exibir os horários;
- `gdelt.enabled` / `gdelt.max_records`: liga a fonte principal e limita os resultados por termo (máximo 250);
- `keywords`: termos pesquisados no GDELT e usados para filtrar os feeds RSS;
- `deduplication.title_similarity_threshold`: similaridade (0–100) a partir da qual dois títulos são considerados a mesma notícia.

## Execução pelo GitHub Actions

O workflow `.github/workflows/daily.yml` roda diariamente às 09:00 UTC (06:00 em
São Paulo), instala as dependências, executa o script e faz commit apenas quando há novo
arquivo ou alteração em `output/`.

Para rodar manualmente: aba **Actions** → workflow **Notícias de IA (diário)** → **Run
workflow**. O campo opcional `target_date` aceita uma data `YYYY-MM-DD`; quando preenchido,
o workflow executa `python main.py --date "$TARGET_DATE"`.

## Onde encontrar o arquivo gerado

Na pasta `output/`, com o nome `IA_YYYYMMDD.md` — por exemplo `output/IA_20260802.md`.

## Limite de requisições do GDELT

A API do GDELT é pública e limita por endereço IP: pede pelo menos 5 segundos entre
chamadas e responde `429` por vários minutos para quem acumula requisições. Por isso o
script agrupa os termos de `keywords` em poucas consultas `OR` (quatro, na configuração
padrão), espera 10 segundos entre elas e, ao receber `429`, aguarda 60s, 120s e 240s antes
de desistir daquela consulta.

Se aparecer a mensagem `GDELT bloqueou o IP por excesso de requisições`, não execute o
script em paralelo nem repetidamente: espere alguns minutos e rode de novo. Mesmo com o
GDELT bloqueado o relatório é gerado com o que vier dos feeds RSS.

## Limitações

Nenhuma fonte consegue garantir literalmente todas as notícias publicadas na internet.
O relatório cobre apenas o que o GDELT indexou e o que os feeds RSS configurados
publicaram, dentro dos termos pesquisados e do limite de registros por consulta.
