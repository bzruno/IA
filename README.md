# IA News

Coletor diário de notícias globais sobre Inteligência Artificial. O script consulta o
[GDELT DOC 2.0](https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/) e feeds RSS
configuráveis, e gera um relatório em Markdown com as notícias publicadas no dia anterior
(fuso `America/Sao_Paulo`), **com o texto integral de cada uma**.

Títulos e textos são mantidos no idioma original. O texto é extraído da página pública da
notícia com o `trafilatura`, que descarta menus, anúncios e comentários. Nada aqui tenta
contornar paywall ou muro de cookies: o que estiver fechado entra no relatório apenas com
os metadados e a marcação `_Texto integral indisponível na fonte._`

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
título ou a descrição casam com os termos da lista `keywords` — ou com um sinal de IA em
outro idioma (`inteligência artificial`, `intelligence artificielle`, `人工知能`, `AI`,
`IA`, `KI`...), para não perder notícias fora do inglês.

Para acrescentar mais um idioma via Google News, use a busca dele:

```text
https://news.google.com/rss/search?q=<termo+codificado>&hl=<idioma>&gl=<país>&ceid=<país>:<idioma>
```

Outros ajustes disponíveis no `config.yaml`:

- `timezone`: fuso usado para definir o dia e exibir os horários;
- `gdelt.enabled` / `gdelt.max_records`: liga a fonte principal e limita os resultados por termo (máximo 250);
- `keywords`: termos pesquisados no GDELT e usados para filtrar os feeds RSS;
- `deduplication.title_similarity_threshold`: similaridade (0–100) a partir da qual dois títulos são considerados a mesma notícia;
- `full_text`: download do texto integral — `enabled`, `max_workers` (downloads simultâneos), `timeout_seconds`, `min_chars` (abaixo disso o texto é tratado como muro de cookies/paywall e descartado) e `max_chars` (corta extrações anormais, como páginas de índice).

Só entram feeds que publiquem o **link direto do veículo**. Agregadores como o Google News
apontam para um redirecionador que não devolve o artigo, então deles não é possível
extrair texto algum.

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
chamadas e responde `429` por cerca de 15 minutos para quem acumula requisições. Por isso o
script agrupa os termos de `keywords` em poucas consultas `OR` (quatro, na configuração
padrão), espera 10 segundos entre elas e, ao receber `429`, aguarda 60s e 120s antes de
desistir. Como o bloqueio vale para a API inteira, ao desistir de uma consulta ele
**cancela as demais** em vez de insistir — o que só prolongaria a punição.

**Em nuvem o bloqueio é frequente.** Runners do GitHub Actions usam faixas de IP de
datacenter compartilhadas com muitos outros usuários do GDELT, então o `429` pode vir já na
primeira requisição, sem culpa da sua execução. É por isso que os feeds RSS do
`config.yaml` incluem buscas do Google News em vários idiomas: eles não têm esse limite e
garantem um relatório útil mesmo com o GDELT indisponível.

Rodando localmente, se aparecer `GDELT bloqueou este IP`, não execute em paralelo nem
repetidamente: espere uns 15 minutos e rode **uma vez**.

## Limitações

Nenhuma fonte consegue garantir literalmente todas as notícias publicadas na internet.
O relatório cobre apenas o que o GDELT indexou e o que os feeds RSS configurados
publicaram, dentro dos termos pesquisados e do limite de registros por consulta.

Duas consequências práticas:

- o GDELT devolve artigos que apenas **citam** IA no texto, então o script exige que o
  título confirme o assunto; notícias de IA com títulos vagos podem ficar de fora;
- feeds RSS guardam poucos itens recentes, então **reprocessar datas antigas depende do
  GDELT**, que mantém cerca dos últimos 3 meses;
- o texto integral depende de a página estar aberta: paywall, muro de cookies ou proteção
  antibot deixam a notícia só com metadados;
- com o texto integral, cada relatório fica na casa de centenas de KB a alguns MB — o
  repositório cresce mais rápido do que com uma lista de links.
