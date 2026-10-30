# Visão funcional: website para pesquisa e encaminhamento de conteúdos

**Estado:** visão de produto para desenvolvimento futuro. Este documento regista o cenário pretendido; não altera o comportamento atual do MCat.

## Objetivo

Disponibilizar uma interface web autenticada para pesquisar conteúdos em fornecedores suportados, apresentar os resultados ao utilizador, permitir selecionar uma ou várias opções e acompanhar o pedido até ao encaminhamento para o grupo destino.

A autenticação do website identifica quem está a usar a aplicação. A autenticação da conta Telegram é uma capacidade separada, descrita em [Ligação futura da conta Telegram](telegram-account-and-destination-selection.md).

## Fluxo principal

1. O utilizador inicia sessão no website.
2. Na página de pesquisa, preenche:
   - termo de pesquisa;
   - tipo de conteúdo: **Músicas/Tracks**, **Albums**, **Playlists** ou **Artistas/Artists**;
   - fornecedor: **Deezer** ou **SoundCloud**.
3. O botão **Pesquisar** fica disponível quando os três campos estão preenchidos com valores válidos.
4. O website mostra os resultados devolvidos, preservando para cada opção o número, título, tipo e informação disponível para a distinguir.
5. O utilizador seleciona uma ou várias opções da mesma pesquisa. A seleção permite combinar números, por exemplo, 1, 3 e 5.
6. O utilizador confirma o pedido e acompanha o processamento de cada opção.
7. Quando o processamento termina, os conteúdos elegíveis são encaminhados para o grupo destino configurado para essa fase do produto.
8. O website mostra o resultado por opção: concluída e encaminhada, já existente, falhada, cancelada ou à espera de uma condição externa.

## Requisitos funcionais

### Pesquisa e seleção

- Exigir uma sessão válida no website antes de permitir pesquisas.
- Validar os campos antes do envio e não submeter uma pesquisa incompleta.
- Apresentar claramente o fornecedor e o tipo de conteúdo escolhidos.
- Permitir selecionar várias opções da mesma lista sem repetir a pesquisa.
- Mostrar a seleção antes da confirmação do pedido.
- Impedir a submissão duplicada do mesmo pedido por duplo clique ou repetição da ligação.
- Preservar os resultados enquanto o utilizador escolhe as opções.

### Processamento e estado

- Criar um pedido rastreável para cada seleção confirmada.
- Apresentar o estado global e o estado individual de cada opção selecionada.
- Indicar a deteção de duplicados sem encaminhar novamente o mesmo conteúdo.
- Mostrar erros, manutenção, limites do fornecedor e ações que requerem intervenção.
- Permitir cancelar um pedido quando o estado do processamento permitir fazê-lo com segurança.
- Não marcar um pedido como concluído até existir confirmação do encaminhamento.

### Destino

Na primeira fase, usar o grupo destino definido pela configuração do serviço, se essa continuar a ser a forma aprovada de operação. A seleção de um grupo entre os grupos acessíveis à conta Telegram do utilizador fica para a fase futura descrita no documento de ligação Telegram.

## Princípios de segurança e privacidade

- Manter as credenciais de acesso ao website separadas das credenciais e sessões Telegram.
- Não apresentar segredos em páginas, registos ou mensagens de erro.
- Autorizar cada utilizador a consultar apenas os seus pedidos e dados.
- Registar alterações de estado e ações relevantes para auditoria, minimizando os dados guardados.
- Aplicar a pausa de manutenção e os limites do bot também aos pedidos iniciados pelo website.
- Separar a escolha de resultados da autorização para encaminhar; a interface deve deixar explícito quando o utilizador confirma uma ação que envia conteúdo ao destino.

## Fases sugeridas

1. **Pesquisa e seleção:** autenticação web, formulário de pesquisa, resultados e seleção múltipla.
2. **Pedidos e acompanhamento:** execução assíncrona, deduplicação, estados, erros e cancelamento seguro.
3. **Destino por utilizador:** ligar uma conta Telegram e permitir escolher um grupo acessível a essa conta, conforme o documento separado.

## Critérios de aceitação iniciais

- Um utilizador autenticado pode pesquisar usando qualquer combinação válida de termo, tipo e fornecedor.
- A pesquisa não pode ser iniciada enquanto faltar um dos campos.
- Os resultados mostram opções identificáveis e permitem selecionar mais de uma.
- A confirmação cria um pedido único e o website apresenta o progresso e o resultado de cada opção.
- Duplicados não são encaminhados novamente.
- As falhas e pausas ficam visíveis e não são apresentadas como sucesso.
- A sessão e os pedidos de um utilizador não ficam acessíveis a outros utilizadores.
