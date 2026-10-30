# Proposta futura: acelerar a pesquisa e a seleção no Telegram

**Estado:** proposta; não altera o comportamento atual.

## Objetivo

Reduzir o tempo entre o envio de uma pesquisa e a confirmação da opção pretendida, automatizando com maior rapidez estes passos:

1. Enviar a pesquisa ao bot.
2. Selecionar o tipo de resultado **Playlists**.
3. Escolher a opção numerada indicada na pesquisa.

O tempo que o próprio bot demora a responder continua fora do controlo do MCat. Esta proposta trata apenas dos intervalos e esperas que a aplicação controla.

## Fluxo atual e observação

O MCat aguarda a resposta da pesquisa, ativa o filtro **Playlists**, aguarda a lista correspondente e seleciona o número pedido. A confirmação dos cliques depende das respostas do Telegram e do estado atualizado da mensagem.

Nos testes, o percurso automático chegou a completar os dois cliques e iniciar a playlist. Numa repetição, o Telegram não confirmou a ativação do filtro dentro do prazo. Por isso, qualquer otimização tem de verificar o estado da mensagem antes de repetir uma ação: um segundo clique cego poderia desligar o filtro ou selecionar um resultado diferente.

Ainda não há medições separadas por etapa. O primeiro passo deve ser recolher esses tempos para distinguir a espera interna do MCat da latência do bot e do Telegram.

## Abordagem proposta

### 1. Medir cada etapa

Registar tempos monotónicos e identificadores da pesquisa para:

- envio da pesquisa até à primeira resposta;
- pedido de ativação de **Playlists** até à confirmação do filtro;
- confirmação do filtro até à lista de opções;
- pedido de seleção numerada até à confirmação de início.

Apresentar no registo um resumo por etapa, sem incluir credenciais nem conteúdo desnecessário das mensagens.

### 2. Reduzir esperas controladas pelo MCat

Rever os intervalos fixos de verificação e substituí-los por espera orientada a eventos, se o cliente Telegram permitir detetar de forma fiável novas mensagens e edições. Como alternativa, usar verificações curtas e adaptativas, com um prazo máximo por etapa.

Assim que a mensagem apresentar o estado esperado, avançar imediatamente para o próximo passo em vez de aguardar o fim de um intervalo fixo.

### 3. Confirmar estado antes de repetir cliques

Depois de um timeout de callback:

- consultar a mensagem original e as mensagens posteriores;
- se o filtro **Playlists** já estiver ativo, continuar sem voltar a clicar;
- se o estado continuar inalterado, permitir no máximo uma repetição controlada;
- se o estado for ambíguo, parar a automação e pedir intervenção em vez de arriscar uma seleção errada.

Aplicar a mesma regra à escolha numerada. Cada ação deve ser associada à mensagem e à pesquisa que a originaram.

## Segurança e limites

- Esta alteração futura não deve armar nem iniciar encaminhamentos.
- Manter os comandos de autorização de encaminhamento separados da pesquisa e seleção.
- Respeitar pausa de manutenção, limites do bot e cancelamento da pesquisa.
- Nunca repetir automaticamente uma ação cujo resultado seja ambíguo.
- Não tentar acelerar a geração ou o descarregamento da playlist pelo bot externo.

## Como validar

Comparar, numa pesquisa pequena e reproduzível, o tempo de cada etapa antes e depois da alteração. Avaliar:

- mediana e percentil 95 da latência interna do MCat por etapa;
- taxa de sucesso da ativação de **Playlists** e da escolha numerada;
- número de timeouts, repetições e interrupções seguras;
- confirmação de que nenhuma pesquisa foi submetida duas vezes e nenhum encaminhamento foi ativado.

Considerar a melhoria aceite quando o tempo controlado pelo MCat diminuir de forma consistente, sem aumentar cliques duplicados, seleções incorretas ou estados ambíguos. A latência do bot deve ser reportada separadamente para não atribuir ao MCat atrasos externos.

## Fora do âmbito

A velocidade de transferência das faixas e o tempo de conclusão do descarregamento pertencem ao bot externo e não são acelerados por esta proposta.
