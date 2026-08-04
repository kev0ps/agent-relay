# CUA Driver — liste des 50 outils MCP

Ce catalogue de référence des outils CUA Driver est consulté dans la documentation officielle :

<https://cua.ai/docs/reference/cua-driver/mcp-tools>

> **Important :** ces 50 noms correspondent au catalogue de référence CUA Driver, pas à une autorisation d'exécution. Le provider runtime fait foi via `tools/list`. Les candidats sont désactivés par défaut et seuls les outils sélectionnés explicitement par l'opérateur sont annoncés puis publiés par Agent Relay. Le nom public v2 est dérivé du provider, par exemple `relay_cua_click`.
>
> Les anciens noms `relay_computer_capture`, `relay_computer_click` et `relay_computer_type` sont conservés ici comme historique de migration ; ils ne constituent plus la surface publique générique v2.

## Légende

- ✅ Outil sélectionné et annoncé publiquement dans une configuration donnée.
- 🔒 Cycle de vie ou diagnostic interne au provider, sans publication automatique.
- 🌓 Candidat du catalogue de référence, désactivé par défaut et soumis au `tools/list` runtime.
- ❌ Outil bloqué par la politique ou non disponible dans le provider configuré.

## 1. Outils d’inspection

| # | Outil CUA Driver | Fonction | État dans Agent Relay |
|---:|---|---|---|
| 1 | `list_apps` | Lister les applications installées ou en fonctionnement. | ❌ |
| 2 | `list_windows` | Lister les fenêtres et leurs identifiants. | 🔒 Interne |
| 3 | `get_window_state` | Récupérer l’état détaillé d’une fenêtre et ses éléments AX/UIA. | 🔒 Interne |
| 4 | `get_accessibility_tree` | Découvrir rapidement les applications et fenêtres du desktop. | ❌ |
| 5 | `get_desktop_state` | Capturer visuellement le desktop complet. | ❌ |
| 6 | `get_screen_size` | Récupérer la taille de l’écran et son facteur d’échelle. | ❌ |
| 7 | `get_cursor_position` | Récupérer la position du curseur. | ❌ |
| 8 | `get_config` | Lire la configuration du driver. | ❌ |
| 9 | `get_recording_state` | Lire l’état de l’enregistrement des trajectoires. | ❌ |
| 10 | `get_agent_cursor_state` | Lire l’état du curseur virtuel de l’agent. | ❌ |

## 2. Outils d’action desktop

| # | Outil CUA Driver | Fonction | État dans Agent Relay |
|---:|---|---|---|
| 11 | `launch_app` | Lancer une application. | ❌ |
| 12 | `kill_app` | Terminer un processus. | ❌ |
| 13 | `bring_to_front` | Mettre une fenêtre au premier plan. | 🔒 Contrat interne Windows |
| 14 | `click` | Cliquer sur un élément sémantique ou à des coordonnées. | 🌓 Candidat via `relay_cua_click` après sélection |
| 15 | `double_click` | Effectuer un double-clic. | ❌ |
| 16 | `right_click` | Effectuer un clic droit. | ❌ |
| 17 | `drag` | Effectuer un glisser-déposer. | ❌ |
| 18 | `type_text` | Insérer du texte dans un élément. | 🌓 Candidat via `relay_cua_type_text` après sélection |
| 19 | `press_key` | Envoyer une touche unique. | ❌ |
| 20 | `hotkey` | Envoyer une combinaison de touches. | ❌ |
| 21 | `set_value` | Modifier directement la valeur d’un élément d’interface. | ❌ |
| 22 | `scroll` | Faire défiler une fenêtre. | ❌ |
| 23 | `move_cursor` | Déplacer le curseur. | ❌ |
| 24 | `zoom` | Capturer une région agrandie d’une fenêtre. | ❌ |

## 3. Outils navigateur CUA Driver

| # | Outil CUA Driver | Fonction | État dans Agent Relay |
|---:|---|---|---|
| 25 | `page` | Outil navigateur legacy multifonction. | ❌ Bloqué ; pas de passthrough arbitraire |
| 26 | `get_browser_state` | Récupérer l’état sémantique du navigateur. | 🌓 Partiel via `browser.snapshot` |
| 27 | `browser_prepare` | Préparer ou vérifier un navigateur isolé. | ❌ |
| 28 | `browser_navigate` | Naviguer vers une URL. | 🌓 Via `relay_browser_navigate` |
| 29 | `browser_click` | Cliquer sur une cible navigateur. | 🌓 Via `relay_browser_click` |
| 30 | `browser_type` | Taper dans une cible navigateur. | 🌓 Via `relay_browser_type` |
| 31 | `browser_dialog` | Gérer les dialogues navigateur. | ❌ |
| 32 | `browser_set_input_files` | Sélectionner des fichiers dans un formulaire web. | ❌ |
| 33 | `browser_download` | Gérer un téléchargement. | ❌ |
| 34 | `browser_pointer` | Effectuer un hover, clic droit, double-clic, scroll ou drag web. | ❌ |

### Sous-actions de `page`

L’outil legacy `page` regroupe notamment les actions suivantes :

- `execute_javascript` — exécuter du JavaScript et retourner le résultat ;
- `get_text` — extraire le texte visible d’une page ;
- `query_dom` — rechercher des éléments avec un sélecteur CSS ;
- `click_element` — cliquer sur un élément sélectionné par CSS ;
- `insert_text` — insérer du texte dans l’élément ayant le focus ;
- `type_keystrokes` — taper du texte avec des événements clavier ;
- `enable_javascript_apple_events` — activer le JavaScript via Apple Events sur macOS.

Ces sous-actions sont regroupées dans un seul outil MCP : elles ne constituent pas sept outils supplémentaires dans le décompte des 50 outils.

## 4. Sessions, enregistrement et maintenance

| # | Outil CUA Driver | Fonction | État dans Agent Relay |
|---:|---|---|---|
| 35 | `start_recording` | Démarrer l’enregistrement des actions et états avant/après. | ❌ |
| 36 | `stop_recording` | Arrêter l’enregistrement. | ❌ |
| 37 | `replay_trajectory` | Rejouer une trajectoire enregistrée. | ❌ |
| 38 | `set_config` | Modifier la configuration du driver. | ❌ |
| 39 | `start_session` | Démarrer une session CUA. | 🔒 Interne |
| 40 | `end_session` | Terminer une session CUA. | 🔒 Interne |
| 41 | `set_agent_cursor_enabled` | Afficher ou masquer le curseur de l’agent. | ❌ |
| 42 | `set_agent_cursor_motion` | Configurer le mouvement du curseur de l’agent. | ❌ |
| 43 | `check_permissions` | Vérifier les permissions système. | ❌ |
| 44 | `health_report` | Récupérer le rapport de santé du driver. | 🔒 Contrat interne Windows |
| 45 | `check_for_update` | Vérifier si une mise à jour du driver est disponible. | ❌ |
| 46 | `install_ffmpeg` | Installer ou préparer FFmpeg pour l’enregistrement vidéo. | ❌ |
| 47 | `verify_state` | Vérifier des prédicats sur l’état d’une fenêtre. | ❌ |
| 48 | `set_agent_cursor_theme` | Choisir le thème du curseur de l’agent. | ❌ |
| 49 | `escalate_session` | Escalader une session vers une phase desktop contrôlée ou une intervention. | ❌ |
| 50 | `get_session_state` | Lire la politique et le périmètre effectif d’une session. | ❌ |

## 5. Publication CUA v2 dans Agent Relay

Agent Relay ne maintient plus une liste fixe de wrappers CUA. Le provider expose
son inventaire runtime avec `tools/list`; le catalogue local borne et classe les
descripteurs, puis l'opérateur sélectionne individuellement les outils autorisés.
La publication MCP utilise des noms stables tels que :

```text
relay_cua_list_windows
relay_cua_get_window_state
relay_cua_click
relay_cua_type_text
```

La présence d'un nom dans ce document ne signifie donc pas qu'il est disponible,
sélectionné ou publiable. Les identifiants de fenêtre, d'accessibilité et de
driver restent la propriété du provider ; Relay ne les génère ni ne les interprète.
Les outils non sélectionnés, indisponibles, malformés ou bloqués ne sont pas
annoncés et ne peuvent pas être appelés via MCP ou WebSocket.

Les résultats CUA passent par le résultat MCP générique borné, qui peut contenir
du texte, des images ou du contenu structuré selon le contrat du provider. Relay
n'expose pas de coordonnées libres, de JavaScript, de handles Playwright, de
processus, de credentials ou d'endpoint provider au client MCP.

## 6. Cycle de vie et opérations sensibles

Le cycle de vie du provider reste local à l'Agent. `start_session` et `end_session`
ne sont pas des options Relay et ne sont pas publiés automatiquement ; ils ne
peuvent devenir des outils publics que comme des descripteurs provider séparés,
sélectionnés explicitement et soumis à la politique de risque. De même,
`bring_to_front` n'est jamais un paramètre caché d'une action : il est soit
provider-interne, soit un outil provider explicite soumis à sélection.

Les classes de risque sont conservées dans le catalogue : inspection en lecture
seule, interaction, action destructive, administration et outil bloqué. Les
opérations de configuration, processus, mise à jour, enregistrement ou
exécution de code restent bloquées ou exigent une politique et un consentement
explicites. `page` reste bloqué lorsqu'il permettrait de transporter
`execute_javascript`, un module, un exécutable ou une méthode arbitraire.

## 7. Règle d'extension

Une extension CUA suit le provider runtime et non une modification d'un wrapper
Relay par opération :

1. vérifier l'outil et son schéma via `tools/list` ;
2. classer son risque et vérifier l'allowlist/politique ;
3. le laisser désactivé par défaut ;
4. le sélectionner explicitement dans l'Agent ;
5. vérifier son nom public, son invocation et son oracle indépendant ;
6. documenter séparément les preuves Browser, CUA Linux et CUA Windows.

L'ajout d'un 51e outil provider ne doit nécessiter aucune modification du
protocole, du catalogue de wrappers ou de la façade MCP. Sa disponibilité réelle
doit être prouvée par l'inventaire runtime et les tests du provider.

## Sources du dépôt Agent Relay

- `src/agent_relay/config.py` — mapping public/interne des outils ;
- `src/agent_relay/protocol.py` — noms et schémas du protocole ;
- `src/agent_relay/capabilities/computer.py` — contrat Cua Driver interne ;
- `README.md` — périmètre public Computer Use ;
- `docs/ROADMAP.md` — limites actuelles concernant screenshots, coordonnées et desktop control.
