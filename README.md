# MCat — Music Catalog PoC

Prova de conceito para observar o bot Telegram Music Downloader, catalogar as mensagens recebidas e testar o encaminhamento para um chat existente.

**Não descarrega, lê, converte nem processa o conteúdo dos ficheiros de áudio.** Quando o encaminhamento está ativado, o Telethon encaminha a própria mensagem Telegram.

## Estado da versão 0.1

- Liga-se à conta Telegram através do Telethon.
- Envia pesquisas ao bot a partir da consola.
- Observa e regista mensagens do bot numa base SQLite.
- Regista mensagens com media sem descarregar os ficheiros.
- Permite ativar/desativar o encaminhamento das próximas mensagens do bot.
- Exporta os registos para CSV.
- Mantém a seleção de modo Playlist e de resultados manual nesta fase.

## Instalação num LXC Debian/Ubuntu

Recomendado: LXC dedicado, Debian 12 ou Ubuntu 24.04, 1 vCPU, 1 GB RAM e 8 GB de disco para a PoC.

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
5. **Antes de clicar no número da playlist**, escreve /arm na consola. Isto ativa o encaminhamento das próximas mensagens recebidas do bot.
6. Clica no resultado da playlist no Telegram.
7. Quando o bot terminar de enviar as músicas, escreve /disarm na consola.
8. Consulta /status e exporta os registos com /export catalogo.csv.

**Cuidado:** enquanto o encaminhamento estiver armado, todas as novas mensagens recebidas desse bot serão encaminhadas para o destino configurado, incluindo eventuais mensagens de texto. Desarma-o assim que a playlist terminar. Faz primeiro o teste com uma playlist pequena e um chat de destino de teste.

## Comandos

- /search <termo> — envia a pesquisa ao bot.
- /tag <etiqueta> — etiqueta as mensagens seguintes.
- /arm — ativa o encaminhamento automático das próximas mensagens do bot.
- /disarm — desativa o encaminhamento.
- /status — mostra contagens.
- /export <ficheiro.csv> — exporta o inventário.
- /help — ajuda.
- /quit — termina.

## Limitações conhecidas

- A seleção de Playlist e de resultados é manual.
- O encaminhamento é controlado explicitamente por /arm e /disarm; não tenta inferir sozinho quando uma playlist começa ou termina.
- O protótipo regista mensagens Telegram e metadados mínimos. Não inspeciona nem descarrega o conteúdo dos ficheiros.
- A deteção de duplicados de texto é apenas heurística. Não elimina mensagens nem versões automaticamente.
- A versão atual encaminha cada mensagem à medida que chega; mensagens agrupadas podem não manter o agrupamento visual original.
- Respeita os limites do Telegram, as condições do bot e os direitos aplicáveis aos conteúdos que obténs e partilhas.

## Segurança

O ficheiro de sessão Telegram dá acesso à conta. Protege-o como uma credencial. O repositório ignora .env, sessões, base de dados e ambiente virtual.
