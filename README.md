# MCat — Music Catalog PoC

Prova de conceito para observar o bot Telegram Music Downloader, catalogar as mensagens recebidas e testar o encaminhamento para um chat existente.

**Não descarrega, lê, converte nem processa o conteúdo dos ficheiros de áudio.** Quando o encaminhamento está ativado, o Telethon encaminha a própria mensagem Telegram.

## Estado da versão 0.2 — recolha por playlist

- Deteta início por `Total Tracks:` e fim por `Finished`.
- Persiste playlists e mensagens multimédia em SQLite e valida contagem.
- Indexa o histórico de GinjMusic para deduplicar pelo ID do documento Telegram.
- `/arm` recolhe em simulação; `/live` autoriza encaminhamento real depois de `Finished`.
- `/music-check` e `/duplicates` consultam o histórico do destino com o filtro oficial `InputMessagesFilterMusic`, usado pela API para procurar mensagens de música. `/music-audit` compara esse filtro com o histórico completo e identifica mensagens com atributos de áudio que ficaram fora do filtro.
- `/duplicates` audita o histórico do destino e lista documentos de áudio repetidos e possíveis correspondências por título/intérprete, sem alterar nem apagar mensagens.
- Uma diferença entre o número anunciado e o observado é reportada, mas não bloqueia automaticamente o processamento.

## Estado da versão 0.1

- Liga-se à conta Telegram através do Telethon.
- Envia pesquisas ao bot a partir da consola.
- Observa e regista mensagens do bot numa base SQLite.
- Regista mensagens com media sem descarregar os ficheiros.
- Permite ativar/desativar o encaminhamento das próximas mensagens do bot.
- Exporta os registos para CSV.
- Mantém a seleção de modo Playlist e de resultados manual nesta fase.

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
    TG_DESTINATION=@nome_do_canal_ou_chat
    DB_PATH=./data/mcat.sqlite3

TG_DESTINATION pode ser um username resolúvel pelo Telegram ou um identificador que a conta consiga aceder. Não partilhes .env, ficheiros .session ou códigos de autenticação.

## Executar

    .venv/bin/python -m mcat

Na primeira execução, o Telegram pede autenticação. A sessão local permite reutilizar a conta nas execuções seguintes.

## Fluxo do primeiro teste

1. Inicia a aplicação e autentica a conta.
2. Na consola, escreve /search Portugal Pop Acústico.
3. No Telegram, seleciona manualmente o modo Playlist.
4. Quando aparecerem os resultados, decide qual playlist queres testar.
5. Escreve `/music-check` para atualizar o índice do histórico de destino, se necessário.
6. Antes de clicar no número da playlist, escreve `/arm`. O MCat recolhe a playlist em modo de simulação por omissão.
7. Clica no resultado da playlist no Telegram e aguarda a mensagem `Finished`.
8. Revê os resumos `[DEDUP]` e `[RESULTADO]`. O `/live` autoriza o encaminhamento real de uma única playlist; não o uses até verificares os números.
9. Usa `/duplicates` para auditar repetições no destino, `/status` para estatísticas ou `/export catalogo.csv` para exportar registos.

**Cuidado:** `/arm` inicia a recolha da playlist em simulação; não envia mensagens. `/live` muda a playlist em curso para encaminhamento real até ao respetivo `Finished`, e o modo de simulação é reposto no final. Testa primeiro numa playlist pequena e verifica o destino antes de usar o modo real.

## Comandos

- /search <termo> — envia a pesquisa ao bot.
- /tag <etiqueta> — etiqueta as mensagens seguintes.
- /arm — arma a recolha de uma playlist em modo de simulação.
- /live — autoriza o encaminhamento real da playlist em curso até `Finished`.
- /disarm — desativa a recolha de novas playlists.
- /music-check — atualiza o índice de mensagens de música no histórico do destino.
- /duplicates — audita duplicados de áudio no destino; só leitura.
- /status — mostra contagens.
- /export <ficheiro.csv> — exporta o inventário.
- /help — ajuda.
- /quit — termina.

## Limitações conhecidas

- A seleção de Playlist e de resultados é manual.
- O MCat identifica o início/fim da playlist através de `Total Tracks:` e `Finished`; a escolha da playlist continua manual.
- A deduplicação exata usa o ID do documento Telegram. A auditoria por título/intérprete é indicativa e pode agrupar edições diferentes da mesma música.
- O protótipo regista mensagens Telegram e metadados mínimos. Não inspeciona nem descarrega o conteúdo dos ficheiros.
- A deteção de duplicados de texto é apenas heurística. Não elimina mensagens nem versões automaticamente.
- A versão atual encaminha cada mensagem à medida que chega; mensagens agrupadas podem não manter o agrupamento visual original.
- Respeita os limites do Telegram, as condições do bot e os direitos aplicáveis aos conteúdos que obténs e partilhas.

## Segurança

O ficheiro de sessão Telegram dá acesso à conta. Protege-o como uma credencial. O repositório ignora .env, sessões, base de dados e ambiente virtual.
