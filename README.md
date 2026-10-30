# MCat — Music Catalog PoC

Prova de conceito para observar o bot Telegram Music Downloader, catalogar as mensagens recebidas e testar o encaminhamento para um chat existente.

**Não descarrega, lê, converte nem processa o conteúdo dos ficheiros de áudio.** Quando o encaminhamento está ativado, o Telethon encaminha a própria mensagem Telegram.

## Estado da versão 0.2 — recolha por playlist

- Deteta início por `Total Tracks:` e fim por `Finished`.
- Persiste playlists e mensagens multimédia em SQLite e valida contagem.
- Indexa os históricos de GinjMusic e deduplica por título, intérprete e duração iguais (normalizados), independentemente do ID do ficheiro Telegram.
- `/arm` recolhe em simulação; `/live` autoriza encaminhamento real depois de `Finished`.
- `/music-check` e `/duplicates` consultam o destino e as fontes opcionais `TG_DEDUP_SOURCES` com o filtro oficial `InputMessagesFilterMusic`; os IDs de documento são unidos para deduplicação entre chats. `/music-audit`, `/music-search-audit` e `/music-detail-audit` continuam a auditar apenas o destino principal.
- `/duplicates` audita todos os chats configurados para redundância e lista documentos de áudio repetidos e possíveis correspondências por título/intérprete, sem alterar nem apagar mensagens.
- `/hist-music-find <termos>` pesquisa título e intérprete nos metadados de áudio de todos os chats configurados, ignorando maiúsculas/minúsculas e acentos; apresenta chat, mensagem e ID do documento. É só leitura.
- Uma diferença entre o número anunciado e o observado é reportada, mas não bloqueia automaticamente o processamento.

## Estado da versão 0.1

- Liga-se à conta Telegram através do Telethon.
- Envia pesquisas ao bot a partir da consola e tenta selecionar automaticamente botões inline `Playlist` e uma opção numerada via callbacks Telegram.
- Observa e regista mensagens do bot numa base SQLite.
- Regista mensagens com media sem descarregar os ficheiros.
- Permite ativar/desativar o encaminhamento das próximas mensagens do bot.
- Exporta os registos para CSV.

## Instalação num LXC Debian/Ubuntu

Recomendado: LXC dedicado, Ubuntu 22.04/24.04 ou Debian 12, 1 vCPU, 1 GB RAM e 8 GB de disco para a PoC.

    apt update
    apt install -y git
    git clone https://github.com/ginjeira/MCat.git
    cd MCat
    bash install.sh

O instalador cria um ambiente virtual Python, instala as dependências e cria .env a partir de .env.example se ainda não existir.

## Configuração

Obtém api_id e api_hash em https://my.telegram.org e edita o ficheiro .env:

    nano .env

Preenche:

    TG_API_ID=123456
    TG_API_HASH=coloca_aqui_o_api_hash
    TG_BOT_USERNAME=nome_de_utilizador_do_bot
    TG_DESTINATION=-1001234567890
    TG_DEDUP_SOURCES=-1234567890
    DB_PATH=./data/mcat.sqlite3

`TG_DESTINATION` é o único chat que recebe novas músicas. `TG_DEDUP_SOURCES` é opcional e aceita IDs/username de chats antigos separados por vírgulas; estes chats são consultados apenas para detetar redundância, nunca recebem encaminhamentos. A conta Telegram tem de conseguir aceder a cada chat. Não partilhes `.env`, ficheiros `.session` ou códigos de autenticação.

## Executar

    .venv/bin/python -m mcat

## Interface web (MVP)

A interface web permite autenticar-se, pesquisar por termo, tipo de conteúdo e fornecedor, escolher várias opções da mesma pesquisa e acompanhar o encaminhamento para o destino configurado no servidor. Nesta fase, a sessão Telegram existente e o destino são partilhados pela instalação; autenticação Telegram por utilizador e escolha individual do grupo destino continuam a ser trabalho futuro.

Configura no ficheiro .env, que deve ser legível apenas pelo utilizador do serviço:

    MCAT_WEB_USERNAME=operador
    MCAT_WEB_PASSWORD=usa-uma-palavra-passe-forte
    MCAT_WEB_HOST=127.0.0.1
    MCAT_WEB_PORT=8080
    MCAT_WEB_COOKIE_SECURE=1
    MCAT_WEB_SSL_CERTFILE=
    MCAT_WEB_SSL_KEYFILE=

Quando a base de dados ainda não tem contas web, MCAT_WEB_USERNAME e MCAT_WEB_PASSWORD criam a primeira conta administradora. As palavras-passe são guardadas como hashes PBKDF2; depois da criação inicial, o administrador pode criar contas adicionais no website e cada utilizador pode alterar a sua palavra-passe. Usa palavras-passe com pelo menos 14 caracteres.

Instala/atualiza as dependências e inicia a interface:

    .venv/bin/pip install -e .
    .venv/bin/mcat-web

O servidor escuta em 127.0.0.1 por omissão. Para acesso na rede local, define MCAT_WEB_HOST para o endereço privado do servidor, configura um certificado e uma chave em MCAT_WEB_SSL_CERTFILE e MCAT_WEB_SSL_KEYFILE e usa uma porta HTTPS, por exemplo 8443. O programa recusa escutar num endereço não local sem HTTPS. Para acesso pela Internet, usa um proxy HTTPS adequado e mantém o serviço MCat inacessível diretamente. MCAT_WEB_COOKIE_SECURE=0 destina-se apenas a desenvolvimento local sem HTTPS.

O serviço web reutiliza a sessão Telegram configurada para o MCat (mcat_user_session por omissão) e o TG_DESTINATION existente. Não executes a interface web e a consola ao mesmo tempo com a mesma sessão. O website não recebe nem guarda códigos Telegram; a autenticação interativa por utilizador e a seleção entre grupos acessíveis ainda não estão implementadas.

Na primeira execução, o Telegram pede autenticação. A sessão local permite reutilizar a conta nas execuções seguintes.

## Fluxo do primeiro teste

1. Inicia a aplicação e autentica a conta.
2. Na consola, escreve `/search Portugal Pop Acústico`. O MCat ativa a recolha em modo de simulação e tenta clicar automaticamente no botão inline `Playlist` e na opção numerada `1`.
3. Para escolher outra opção numerada, indica-a no fim: `/search Portugal Pop Acústico 2`.
4. Aguarda a mensagem `Finished` e revê os resumos `[DEDUP]` e `[RESULTADO]`.
5. Usa `/music-check` para atualizar o índice dos históricos, se necessário.
6. O `/live` autoriza o encaminhamento real de uma única playlist; não o uses até verificares os números.
7. Usa `/duplicates` para auditar repetições no destino, `/status` para estatísticas ou `/export catalogo.csv` para exportar registos.

**Cuidado:** `/arm` inicia a recolha da playlist em simulação; não envia mensagens. `/live` muda a playlist em curso para encaminhamento real até ao respetivo `Finished`, e o modo de simulação é reposto no final. Testa primeiro numa playlist pequena e verifica o destino antes de usar o modo real.

## Comandos

- /search <termo> [n] — envia a pesquisa, tenta clicar em `Playlist` e escolhe automaticamente a opção numerada `n` (por omissão, `1`); ativa recolha em modo de simulação.
- /tag <etiqueta> — etiqueta as mensagens seguintes.
- /arm — arma a recolha de uma playlist em modo de simulação.
- /live — autoriza o encaminhamento real da playlist em curso até `Finished`.
- /disarm — desativa a recolha de novas playlists.
- /music-check — atualiza os índices de música do destino e das fontes adicionais de redundância.
- /duplicates — audita duplicados de áudio em todos os chats usados para redundância; só leitura.
- /hist-music-find <termos> — pesquisa artista/título nos históricos configurados; só leitura.
- /status — mostra contagens.
- /export <ficheiro.csv> — exporta o inventário.
- /help — ajuda.
- /quit — termina.

## Limitações conhecidas

- A seleção automática depende de o bot apresentar botões inline com o texto `Playlist` e opções numeradas reconhecíveis. Se a estrutura ou os rótulos dos botões forem diferentes, o clique automático pode não acontecer; confirma as mensagens `[AUTO-CLICK]` na consola.
- A implementação dos cliques automáticos ainda precisa de validação com a sessão Telegram real. O modo de simulação continua a ser o comportamento inicial e não encaminha músicas.
- O MCat identifica o início/fim da playlist através de `Total Tracks:` e `Finished`; playlists incompletas geram aviso, mas não são bloqueadas.
- A deduplicação segue a lógica do Spotygram: considera duplicados o mesmo documento Telegram ou faixas com título, intérprete, duração e tamanho do ficheiro iguais (título/intérprete normalizados). Registos sem metadados suficientes não são agrupados por metadados.
- O protótipo regista mensagens Telegram e metadados mínimos. Não inspeciona nem descarrega o conteúdo dos ficheiros.
- A deteção de duplicados de texto é apenas heurística. Não elimina mensagens nem versões automaticamente.
- A versão atual encaminha cada mensagem à medida que chega; mensagens agrupadas podem não manter o agrupamento visual original.
- Respeita os limites do Telegram, as condições do bot e os direitos aplicáveis aos conteúdos que obténs e partilhas.

## Segurança

O ficheiro de sessão Telegram dá acesso à conta. Protege-o como uma credencial. O repositório ignora .env, sessões, base de dados e ambiente virtual.

