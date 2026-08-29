# IA News

Coletor diário de notícias globais sobre Inteligência Artificial. O script consulta o
[GDELT DOC 2.0](https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/) e feeds RSS
configuráveis, e gera um relatório em Markdown com **as notícias mais relevantes** do dia
anterior (fuso `America/Sao_Paulo`), **com o texto integral de cada uma**.

O relatório é enxuto por definição: no máximo **15 notícias**, escolhidas por impacto —
lançamento de modelo, aquisição, rodada de investimento, decisão regulatória, incidente de
segurança, infraestrutura de chips e data centers. Podcast, vídeo, coluna de opinião, carta
do leitor, tutorial, listão e conteúdo promocional ficam de fora, mesmo quando falam de IA.
Ver [Como as notícias são escolhidas](#como-as-notícias-são-escolhidas).

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

Feeds com `enabled: false` são ignorados. Itens de RSS só entram no relatório quando **o
título** casa com os termos da lista `keywords` — ou com um sinal de IA em outro idioma
(`inteligência artificial`, `intelligence artificielle`, `人工知能`, `AI`, `IA`, `KI`...),
para não perder notícias fora do inglês. A descrição não conta: aceitá-la fazia entrar
matéria de baleia, futebol ou LibreOffice que só citava IA de passagem no meio do texto.

Feeds cujo escopo já é IA (o recorte de IA de um veículo, ou o blog de uma empresa de IA)
podem declarar `ai_only: true` e dispensam esse teste:

```yaml
  - name: OpenAI
    url: https://openai.com/news/rss.xml
    enabled: true
    ai_only: true
```

Sem essa marca, um título como *"Introducing Claude Opus 5"* seria descartado por não
conter nenhuma outra palavra que confirme o assunto.

Para acrescentar mais um idioma via Google News, use a busca dele:

```text
https://news.google.com/rss/search?q=<termo+codificado>&hl=<idioma>&gl=<país>&ceid=<país>:<idioma>
```

Outros ajustes disponíveis no `config.yaml`:

- `timezone`: fuso usado para definir o dia e exibir os horários;
- `report.max_articles`: teto de notícias publicadas por dia (padrão 15);
- `report.min_score`: nota mínima de relevância para uma notícia entrar, mesmo sobrando vaga (padrão 3);
- `gdelt.enabled` / `gdelt.max_records`: liga a fonte principal e limita os resultados por termo (máximo 250);
- `keywords`: termos pesquisados no GDELT e usados para filtrar os feeds RSS — termos com menos de 4 caracteres (`LLM`, `xAI`) valem só para o RSS, porque a GDELT rejeita a consulta inteira quando um deles é curto demais;
- `deduplication.title_similarity_threshold`: similaridade (0–100) a partir da qual dois títulos são considerados a mesma notícia;
- `full_text`: download do texto integral — `enabled`, `max_workers` (downloads simultâneos), `timeout_seconds`, `min_chars` (abaixo disso o texto é tratado como muro de cookies/paywall e descartado) e `max_chars` (corta extrações anormais, como páginas de índice).

Só entram feeds que publiquem o **link direto do veículo**. Agregadores como o Google News
apontam para um redirecionador que não devolve o artigo, então deles não é possível
extrair texto algum.

## Como as notícias são escolhidas

Todo dia sobram muito mais candidatas do que cabem no relatório — já houve dias com mais de
90 itens, a maioria irrelevante. A seleção acontece em quatro etapas.

**1. Descarte de formato.** O que não é notícia sai antes de qualquer pontuação, por
casamento de padrão no título e no caminho da URL: podcast, vídeo, áudio, cobertura ao
vivo, newsletter, webinar, coluna de opinião, editorial, carta do leitor, entrevista,
tutorial, guia, listão (`5 ways to...`), resenha de produto, promoção e chamada de evento.
As listas ficam em `NOISE_TITLE_PATTERNS` e `NOISE_PATH_PATTERNS`, no `main.py`.

**2. Agrupamento da mesma história.** Além da similaridade de título, duas notícias são
tratadas como a mesma quando compartilham **duas âncoras** — organização citada ou valor
numérico — **e** o mesmo tipo de acontecimento. É o que junta *"Nvidia agrees to buy Hugging
Face for $12.9 billion"* com *"Nvidia übernimmt Hugging Face für 12,9 Milliarden Dollar"*,
que nenhuma comparação de texto casaria. Exigir o tipo de acontecimento em comum evita colar
duas notícias diferentes que apenas citam as mesmas empresas. Entre as versões de uma mesma
história, publica-se a da fonte de faixa mais alta.

**3. Nota de relevância.** Cada história soma:

| Sinal | Peso |
| --- | --- |
| Lançamento de produto, modelo ou padrão (`launches`, `公開`, `stellt vor`, `lança`...) | +4,0 |
| Modelo novo ou nova versão (família de modelo + número: `Gemini 1.1`, `Claude Opus 5`) | +4,0 |
| Aquisição, fusão, rodada de investimento, cifra bilionária | +3,5 |
| Regulação, tribunal, processo, proibição, controle de exportação | +3,0 |
| Incidente de segurança, ataque, vazamento, uso indevido | +2,5 |
| Pesquisa, estudo, avanço científico, benchmark | +2,5 |
| Infraestrutura: chips, semicondutores, data centers, energia | +2,0 |
| Resultado financeiro, expansão de operação, parceria, demissões | +2,0 |
| Cada organização de peso citada no título (até 3) | +1,0 |
| Faixa da fonte (`sources.tiers`) | +4 a −4 |
| Cada veículo extra que noticiou o mesmo caso (até 3) | +2,0 |

A soma das categorias é limitada a 10 pontos, para que um acúmulo de palavras-chave não
supere uma notícia de fato importante. Os pesos e listas de termos estão em `EVENT_SIGNALS`
e `MAJOR_ENTITIES`, no `main.py`; os limites, em `report`, no `config.yaml`.

**4. Corte.** Ficam as `report.max_articles` melhores com nota igual ou acima de
`report.min_score`, ordenadas da mais para a menos relevante. Em dia fraco o relatório sai
com menos de 15 — o teto é limite, não cota.

O relatório mostra a nota de cada notícia e, quando houve agrupamento, quantos veículos
noticiaram o caso.

## Apenas veículos oficiais

O bloco `sources` do `config.yaml` restringe o relatório a domínios conhecidos — agências,
jornais, veículos de tecnologia estabelecidos e os blogs oficiais das empresas de IA:

```yaml
sources:
  official_only: true
  allowlist:
    - reuters.com
    - bbc.com
    - openai.com
```

A regra vale para **as duas fontes**: o GDELT indexa milhares de sites, e sem essa lista
ele traz muito portal desconhecido. Subdomínios são aceitos automaticamente
(`g1.globo.com` entra por `globo.com`). Para aceitar qualquer domínio, use
`official_only: false`.

Além de barrar o que está fora, o bloco `sources.tiers` pontua o que está dentro:

| Faixa | Peso | Quem entra |
| --- | --- | --- |
| `empresa` | +4 | a empresa anunciando o próprio produto (openai.com, anthropic.com, nvidia.com...) |
| `agencia` | +3 | agências e jornais de referência (Reuters, AP, Bloomberg, FT, BBC, CNBC...) |
| `tecnologia` | +1 | imprensa especializada (TechCrunch, The Verge, Wired, Ars Technica...) |
| `tutorial` | −4 | blogs de tutorial de produto (aws.amazon.com) — material técnico útil, mas não é notícia |

Domínios da `allowlist` fora de qualquer faixa usam `default_weight`. Quando um domínio
casa com mais de uma faixa, vence o mais específico: `aws.amazon.com` pesa como `tutorial`
mesmo com `amazon.com` na faixa `empresa`.

Alguns veículos importantes não têm mais RSS público — verificado em agosto de 2026:

| Veículo | Situação | Como entra no relatório |
| --- | --- | --- |
| Reuters, AP | RSS encerrado (404/401) | pelo GDELT, via `allowlist` |
| CNN | RSS existe, mas parado desde 2016 | pelo GDELT, via `allowlist` |
| NYT, Bloomberg, FT, Le Monde | feed ativo, texto bloqueado por paywall | fora do relatório |
| Anthropic, Mistral, xAI | não publicam RSS | pelo GDELT, via `allowlist` |

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
primeira requisição, sem culpa da sua execução. Por isso os dois coletores são
independentes: se o GDELT cair por inteiro, o relatório sai com o que os feeds RSS
trouxeram — e são eles que cobrem os veículos e blogs oficiais mais importantes.

Rodando localmente, se aparecer `GDELT bloqueou este IP`, não execute em paralelo nem
repetidamente: espere uns 15 minutos e rode **uma vez**.

### Alternativas ao GDELT

| Alternativa | Custo | Link direto? | Observação |
| --- | --- | --- | --- |
| **RSS dos próprios veículos** | grátis | sim | é o que este projeto usa; sem limite de requisição |
| NewsAPI.org | chave; grátis só para desenvolvimento | sim | plano gratuito atrasa as notícias em 24h |
| GNews.io | chave; 100 consultas/dia | sim | cobertura menor |
| Event Registry, NewsCatcher | pago | sim | cobertura ampla, multilíngue |
| Google News RSS | grátis | **não** | link de redirecionamento: impede extrair o texto |
| Common Crawl (CC-NEWS) | grátis | sim | arquivos WARC de dezenas de GB por dia |

Na prática, o RSS dos veículos substitui o GDELT para as fontes que têm feed, e o GDELT
continua útil justamente para as que não têm (Reuters, AP, CNN, Anthropic).

## Limitações

Nenhuma fonte consegue garantir literalmente todas as notícias publicadas na internet.
O relatório cobre apenas o que o GDELT indexou e o que os feeds RSS configurados
publicaram, dentro dos termos pesquisados e do limite de registros por consulta.

Consequências práticas:

- o GDELT devolve artigos que apenas **citam** IA no texto, então o script exige que o
  título confirme o assunto; notícias de IA com títulos vagos podem ficar de fora;
- a nota de relevância lê o **título**, não o texto: uma notícia importante com manchete
  criativa pode pontuar baixo e perder a vaga para outra mais explícita;
- o teto de 15 é deliberado — em dia movimentado, boas notícias ficam de fora. Para ver
  mais, aumente `report.max_articles`;
- feeds RSS guardam poucos itens recentes, então **reprocessar datas antigas depende do
  GDELT**, que mantém cerca dos últimos 3 meses;
- o texto integral depende de a página estar aberta: paywall, muro de cookies ou proteção
  antibot deixam a notícia só com metadados.
