# README2 — Lecciones aprendidas y trampas conocidas

Compañero de `README.MD`. `README.MD` es la **especificación** (qué debe hacer el
addon). Este documento es el **conocimiento operativo** acumulado al implementarla:
qué rompe el addon, por qué, y qué no volver a intentar.

Todo lo de aquí salió de una sesión real de depuración con dos peers. Las cuatro
regresiones de la sección 5 las introduje yo por actuar sobre teoría sin confirmar;
están documentadas para que nadie las repita.

---

## 1. Reglas de oro (no negociables)

### 1.1 Los timelines son independientes

`README.MD` §3.1 y §7.1: **bajo ninguna circunstancia** se pausa, detiene o altera
el timeline del Cliente. Prohibido `frame_set()` y `is_animation_playing` como
parte del flujo de Live Sync.

El keyframe temporal se escribe en el **frame del Host** (§1.2, §7.5). El revisor
reproduce su propio bucle y ve la edición en contexto cuando su playhead pasa por
ese frame. Eso es justamente lo que permite evaluar "las transiciones de entrada y
salida de la pose editada dentro del flujo completo de la animación" (§1.3).

> **Error cometido:** implementé `_follow_frame()` que asignaba
> `scene.frame_current` en el Cliente. Hizo que el timeline del Cliente saltara al
> frame del Host y destruyó el propósito del addon. Revertido el mismo día.
> `test_client_timeline_is_never_altered` lo fija.
>
> Consecuencia importante: **"el Cliente no se actualiza cuando el Host scrollea"
> es comportamiento correcto**, no un bug. No "arreglarlo" moviendo el playhead.

El único `frame_current =` legítimo del addon está en `operators.py`, dentro de un
modal gated por `session.user_snap_running` que el usuario activa a mano. No es
parte de Live Sync.

### 1.2 El Cliente es un receptor pasivo

El Cliente no commitea ni pushea. **Pero porcelain no lo garantiza:**
`Repository.assert_modification_rights` acepta `owner in [username, RP_COMMON]`, o
sea que para la librería COMMON significa "cualquiera puede". La regla se impone en
`handlers.can_publish()`, que rechaza a cualquier peer que no sea Host **antes** de
mirar propiedad o `check_common`.

> **Error que esto evitó:** el Cliente estaba commiteando y pusheando Collections
> (`bl_check_common = True`). El Host las aplicaba, `BlCollection.load` desarmaba y
> rearmaba objetos, y el log del Host se llenaba de
> `Post view widget exception: StructRNA of type Object has been removed`. El
> armature perdía el enlace a su Action y todos los keyframes del viewer
> desaparecían hasta un push forzado completo.

### 1.3 La selección no es exclusiva

Varios usuarios pueden tener el mismo objeto seleccionado a la vez.
`DynamicRightSelectTimer` **publica** la selección (`update_user_metadata` con
`selected_objects`, de eso vive el overlay de presencia) pero **no bloquea**: los
`porcelain.lock`/`unlock` por selección se quitaron a pedido explícito.

### 1.4 El pseudocódigo del README es ilustrativo

Cuando el pseudocódigo contradice la prosa o las restricciones, mandan la prosa y
las restricciones. El spec se escribió contra un layout ZMQ / `libs/replication/`
que ya no existe; seguirlo literal produce código que no corre.

### 1.5 Un keyframe temporal por hueso editado

§7.5: no crear keyframes en cada frame del bucle del Cliente. Solo se manipula **un**
keyframe temporal por hueso editado, en el frame específico del Host.

---

## 2. Trampas de la API de Blender

Verificadas por sondeo directo en **4.5.2 LTS y 5.2.2 LTS** (idénticas en ambas).

### 2.1 Rotación

| Hecho | Consecuencia |
|---|---|
| `PoseBone.rotation_quaternion` es el nombre real. **`rotation_quat` no existe.** | Un `data_path` de F-Curve es una ruta Python real: keyear `pose.bones["X"].rotation_quat` crea curvas que **nada evalúa**. Así la rotación en quaternion nunca sincronizaba y en Euler sí. |
| `matrix_basis` no representa más de una vuelta completa | `Matrix.decompose()` normaliza euler a [-180, 180]. Host 270° → viewer -90°. Misma pose visible, valor almacenado distinto, reproducción incorrecta. |
| 270° y 630° producen la **misma** matriz | Detectar movimiento comparando matrices pierde vueltas completas. Comparar el payload de canales, no la matriz. |

Por eso la rotación viaja como **canales crudos** (`location`, `rotation_mode`,
`rotation`, `scale`), nunca como matriz descompuesta. `matrix_basis` se sigue
enviando solo para el fallback sin Action y para viewers en build viejo.

Convertir entre modos distintos pasa por quaternion y **pierde el conteo de
vueltas**. Solo es lossless cuando ambos peers tienen el mismo `rotation_mode`.

### 2.2 Detección de operadores en ejecución

| Hecho | Consecuencia |
|---|---|
| `wm.operators` es un **histórico acumulativo** | Una sesión real llegó a ~45 entradas, incluidas 20 invocaciones terminadas de `SessionToggleLiveSync`. Escanear todo el stack reporta como activo un drag que terminó hace minutos. |
| Solo `operators[-1]` puede estar corriendo | Y aun así persiste tras terminar. Por eso hace falta combinarlo con **movimiento real** más un timeout (`_motion_stalled()`). |
| `type(op).__name__` devuelve `'Operator'` para todo operador C | Inútil para identificar. La identidad fiable es `op.bl_rna.identifier` (`TRANSFORM_OT_trackball`); `bl_idname` da la forma punteada. |
| La tecla R puede correr `TRANSFORM_OT_trackball` | No `transform.rotate`. Depende del método de rotación del keymap. Cualquier lista de nombres debe incluirlo. |

### 2.3 No hay forma barata de saber qué canales editó el usuario

Probado y descartado, ambos:

- **Snapshot de `_begin`**: se toma al *seleccionar*. Si el Host selecciona en el
  frame 40 y scrollea a 50, la pose difiere legítimamente sin edición → reportaría
  edición falsa.
- **Pose evaluada vs. `bpy.data`**: durante un transform modal Blender escribe
  **directo en `bpy.data`** (por eso Esc revierte vía undo). Son iguales mientras
  arrastras → no detecta nada.

> **Error cometido:** usé la segunda comparación para enviar un mapa `edited` por
> canal. Dio vacío siempre durante una edición real y el overlay dejó de keyear
> **por completo**. Revertido.

La fuente correcta sería `op.properties.constraint_axis` del operador de transform
en ejecución. No está implementado; no intentarlo por comparación de poses.

### 2.4 Actions y slots en Blender 5

| Hecho | Consecuencia |
|---|---|
| Asignar `animation_data.action` **no** pobla `action_slot` | `action_slot` puede ser `None` legítimamente con action puesto. Leer `.identifier` sin guardar lanzaba `AttributeError`, escapaba de `SessionListenTimer.execute()` y mataba la sesión. |
| `Action.fcurves` fue **removido** en 5.x | Las curvas viven en channelbags por slot vía `bpy_extras.anim_utils`. `bl_types/bl_action.py` ya bifurca por `bpy.app.version >= (5,0,0)`. |
| `action_get_channelbag_for_slot` devuelve `None` | Si el slot no tiene channelbag. Leer `.fcurves` sobre `None` tumba la sesión igual que arriba. |
| `slots.new(target_id_type, name_display)` **genera un identifier nuevo** | El `identifier` nunca sobrevive un round trip. Emparejar slots por `(target_id_type, name_display)`. |
| `Action.id_root` no existe en 5.2 | `dump()` no lo emite y `load()` lo guarda con `.get()`, así que es inerte. |

### 2.5 Colecciones y wrappers

- Mutar un `bpy_prop_collection` **mientras se itera salta entradas**. Iterar sobre
  `list(...)` siempre.
- Los wrappers de `Keyframe` y `F-Curve` quedan **stale** en cuanto se muta la
  colección. Re-resolver por `data_path`/frame después de cualquier mutación. Esto
  rompe tests de forma silenciosa **y dependiente de versión**: un check pasó en 4.5
  y falló en 5.2.2 por un wrapper stale.
- `bpy.app.timers` sufre throttling cuando la ventana no tiene foco. Un viewer en
  segundo plano solo procesa al pasar el mouse por encima. **No es bug del addon** y
  no se puede arreglar desde acá.

---

## 3. Trampas de `replication` / `porcelain`

### 3.1 La máquina de estados

Estados: `ADDED=0, COMMITED=1, PUSHED=2, FETCHED=3, UP=4, MODIFIED=5, ERROR=6`.

| Función | Gate | Efecto |
|---|---|---|
| `commit` | `state in [ADDED, UP]` | Si no, loguea `Commit skipped: data in a wrong state` y retorna. **El warning no nombra el estado.** |
| `commit` | además requiere diff real | Solo pasa a `COMMITED` si `delta.diff` no es vacío. |
| `push` | `state == COMMITED` salvo `force=True` | Es lo **único** que devuelve un nodo a `UP`. |

**Un nodo en `COMMITED` NO está varado**: `push` lo acepta tal cual. Solo los
estados fuera de `ADDED`/`UP`/`COMMITED` necesitan `force`.

> **Error cometido:** puse `force=True` incondicional en
> `push_animation_dependencies`. Un push forzado envía el **nodo completo** en vez
> del delta, así que cada viewer ejecutaba `BlAction.load` y reconstruía el Action,
> destruyendo el keyframe temporal que el streamer acababa de escribir. Scrollear el
> timeline del Host borraba el overlay en cada frame.
>
> Correcto: `force=stranded` donde
> `stranded = dep.state not in (ADDED, UP, COMMITED)`.

### 3.2 Propiedad

- `porcelain.add` usa `default_owner = RP_COMMON`, y los hijos heredan
  (`add(..., owner=new_owner)`). Casi todo nodo nace COMMON.
- `assert_modification_rights` acepta `[username, RP_COMMON]`.
- `bl_check_common = True` en **Collection, Scene y World**; `False` en el resto,
  incluido `BlAction`.
- Combinación letal ya ocurrida: Action COMMON + `bl_check_common = False` + gate
  `node.owner == username or check_common` ⇒ **ningún** peer podía publicar un
  keyframe. Por eso insertar, mover y borrar keyframes nunca llegaba al viewer.

### 3.3 `Timer.main()` convierte cualquier excepción en desconexión

```python
except Exception as e:
    self.unregister()
    session.disconnect(reason=f"Error during timer {self.id} execution")
```

Un `AttributeError` profundo en un `dump` se manifiesta como "la sesión se cayó
sola". **Cuando una sesión muere sin razón visible, buscar el último `CLIENT ERROR`
antes de `Exiting session`.**

### 3.4 Recepción

- `interface.py` hace `do_commit(update)` y luego `node.state = FETCHED` en cada
  update en vivo. `do_commit` **no** asigna FETCHED por sí solo.
- `do_commit` descarta un delta si el receptor no tiene el nodo
  (`Skipping update`).
- `do_commit` reemplaza `node.data` por un **objeto nuevo** (`patch` hace
  `self.data = self.data + commit.delta`). Por eso la **identidad del objeto** es un
  test fiable de "llegó algo nuevo" — es la base de la guarda anti-loop de
  `ApplyTimer`.
- El servidor **excluye al remitente** (`client_uid != identity`), así que no hay eco
  de doble aplicación.

### 3.5 El delta de deepdiff funciona — no volver a investigarlo

`BlAction` no define `compute_delta`; hereda `Delta(DeepDiff(old, new, cache_size=5000))`.
Pese a que `dump_fcurve(use_numpy=True)` guarda numpy, el encadenado round-tripea
correctamente para **insertar, mover y borrar** keyframes, incluso acarreado
`node.data` a través de `patch` como hace porcelain.

Probado por `test_action_delta_chain_roundtrip` y
`test_action_delta_roundtrip_move_and_delete`. Si un keyframe no llega al viewer, el
problema **no** está en el delta.

---

## 4. Trampas de arquitectura del addon

### 4.1 `applied_updates` solo para cambios replicados

El marcador significa "este update del depsgraph lo causé yo aplicando datos
remotos, no lo republiques". `on_scene_update` consume un marcador por uuid y salta
el commit/push.

**Invariante:** solo marcar cambios que estén en el `include_filter` del dump de ese
tipo. Si el campo no se replica, no hay eco que suprimir, y como un flag de display
no necesariamente tagea el depsgraph, **nadie consume el marcador**: se queda para
siempre y el siguiente edit genuino del usuario se descarta como dato remoto.

> **Error ocurrido:** `DynamicRightSelectTimer` marcaba tras cambiar `obj.hide_select`.
> `hide_select` no está en ningún filtro de `BlObject.dump`. Síntoma: "el toggle
> armado se comporta como un botón normal".

Lo mismo aplica a `ApplyTimer`: si `porcelain.apply` lanza, hay que **quitar** el
marcador, porque el nodo queda FETCHED y se reintenta en cada tick.

### 4.2 El overlay y el reload del Action se pisan

`BlAction.load` reconstruye el Action. Tras eso, los wrappers de fcurve y los
valores respaldados que guarda el overlay están **muertos**.

- `invalidate_overlays()` descarta el bookkeeping **sin restaurar**. Es lo correcto
  tras un reload.
- `cleanup()` **restaura**, y por eso es incorrecto en ese punto: escribiría valores
  viejos sobre las curvas recién aplicadas.
- Descartar sin restaurar **tampoco** vale si el reload no limpia las curvas: el
  keyframe temporal queda huérfano y se ve como "confirmado". Por eso hace falta el
  podado de 4.3.

### 4.3 `load_fcurve` solo limpia lo que el dump trae

`load_fcurve` borra todos los `keyframe_points` **de la curva que recibe**. Pero
solo se llama sobre las curvas presentes en el dump. Las curvas que el overlay
**inventó** (location y scale cuando el Host solo keyeó rotación: payload de 3
curvas contra 9 canales escritos) sobreviven.

Por eso `BlAction.load` poda del channelbag toda curva que el dump no trae. El dump
es la verdad completa de su slot.

**El podado cubre solo la rama de Blender 5.** La rama 4.x sigue sin podar y
arrastra un quirk preexistente: itera `data["fcurves"]` una vez por slot.

### 4.4 El overlay SÍ debe crear curvas faltantes

`test_pose_overlay` fija `missing rotation/scale curves created`. Si el Action del
viewer no tiene la curva que el Host está editando, sin creación no se vería nada.

> **Error cometido:** hice que `_write_pose` salteara canales sin curva existente, y
> después que salteara canales "sin cambio" comparando contra `bone.location` /
> `rotation_euler` / `scale`. Esa base es el valor en el frame del **viewer**, y el
> overlay escribe en el frame del **Host**: con timelines independientes son frames
> distintos por diseño. Fallaron 2 tests. Revertido.

### 4.5 `start` se emite una vez por selección

El stream queda abierto y solo fluyen `update`. Un viewer que conecta después
**nunca** oye el `start`, y `_on_update` descartaba todo en silencio. Log real: ~40 s
de `viewer applied 1/N [update=N]` sin ninguna línea `keying`.

Dos piezas lo resuelven, y hacen falta **ambas**:

- `_revive_overlay()` reconstruye el overlay desde un `update` (trae armature uuid,
  bone name, frame y matriz: todo lo que un `start` tiene). Se rechaza si la clave
  está en `ClientPoseApplier._ended`, para que un update residual en vuelo no
  resucite un stream terminado.
- `HEARTBEAT_INTERVAL` reenvía la pose en reposo. Sin heartbeat, un viewer tardío no
  recibe **nada** hasta que el Host mueva algo — que es exactamente el síntoma "el
  keyframe temporal solo aparece cuando empiezo a transformar".

Un heartbeat **no debe** contar como movimiento (`_last_motion`): si contara,
`_motion_stalled()` nunca sería True, `transform_active` quedaría encendido con
cualquier operador residual en el stack, y `pose_stream_active()` bloquearía todos
los push armados.

### 4.6 `pose_stream_active()` debe ser más estrecho que "streaming"

Devuelve `streamer.transform_active` (= `INTERACTING` **y** operador de transform
presente **y** sin `_motion_stalled()`), **no** `state == INTERACTING`.

Si devolviera el estado, el guard de README §7.8 en `on_scene_update`
(`if is_live and live_sync.pose_stream_active(): continue`) bloquearía todos los
toggles armados mientras el usuario tenga un hueso seleccionado.

Fijado por `a selected-but-not-dragged bone does not suppress porcelain`.

### 4.7 Dónde corre realmente el commit

`push_animation_dependencies` solo se llama si el nodo **padre** está armado, y solo
recorre dependencias Action cuyo owner sea el usuario o `RP_COMMON`. Si el Action
llega directo al batch del depsgraph, el commit ocurre en el **loop principal** de
`on_scene_update`.

> **Error cometido:** puse el log `committed Action` dentro de
> `push_animation_dependencies`. El Action llegaba por el loop principal, así que el
> log no aparecía y concluí mal que nunca se commiteaba. Instrumentar en el lugar
> equivocado es tan dañino como no instrumentar.

### 4.8 Otros

- `BlObject.load` llama `load_animation_data(...)` como **primera** sentencia; el
  bloque `# Parenting` va ~35 líneas después. Un raise en el paso de animación deja
  el objeto sin padre ("el cubo se desconectó del hueso").
- `bl_reload_parent` / `bl_reload_child` en `ApplyTimer` re-aplican nodos con
  `force=True`. Ambos caminos deben contar como "Action recargado" a efectos de
  invalidar el overlay.
- `ContinuousActionPushTimer` está definido en `timers.py` pero **nunca se registra**
  en `deleyables`. Es intencional: el push es por eventos vía `on_scene_update`;
  un polling a 0.4 s duplicaría los push.
- `ApplyTimer` corre a `settings.depsgraph_update_rate` (default **1 segundo**,
  expuesto en el panel como "Apply delay"). `SessionListenTimer` corre a 0.001 s.
- `session.is_host` lo pone `SessionHostOperator` (`wm.session_host`) tras un
  `session.host()` exitoso. Sin eso todos los peers salen `role=VIEWER` y no existe
  broadcaster.

---

## 5. Lecciones de método

### 5.1 Las cuatro regresiones que introduje por teorizar

| # | Cambio | Por qué estaba mal |
|---|---|---|
| 1 | `_follow_frame()` asignaba `scene.frame_current` en el Cliente | Violaba la regla de oro §1.1. Además **enmascaró** el bug del viewer tardío. |
| 2 | `force=True` incondicional en `push_animation_dependencies` | Push completo en cada update ⇒ el viewer reconstruía el Action y perdía su overlay en cada frame scrolleado. |
| 3 | `_values_equal` contra `bone.location`/`rotation_euler`/`scale` | Base equivocada: valores en el frame del viewer, no en el frame del Host donde escribe el overlay. |
| 4 | `edited` comparando pose evaluada vs `bpy.data` | Durante un transform modal Blender escribe en `bpy.data`, así que dio vacío siempre y el overlay dejó de keyear por completo. |

Regla derivada: **leer los logs antes de cambiar código.** Prácticamente todo lo que
se resolvió en esta sesión se resolvió con evidencia, no con razonamiento.

### 5.2 Verificar que el build instalado tiene los cambios

El usuario instala el repo como `.zip` en un Blender portable. Los edits no tienen
efecto hasta re-empaquetar y reinstalar.

```
grep por una cadena firma en:
  portable\extensions\user_default\multi_user\
```

"El fix no funciona" suele ser "todavía no se reinstaló". Y ojo: `fc /b` no sirve
para comparar (el repo es LF y la copia instalada CRLF); `ripgrep` salta
directorios con punto, así que `.user\...` no se puede grepear — pero ahí solo hay
`cache`, no código.

### 5.3 Trampas del harness de tests

- **`check()` devuelve `None`.** Escribir `if not check(cond, ...): return` es
  siempre True y el test sale **antes de afirmar nada**, pasando de forma vacua.
  Usar `check(...)` y luego un `if` separado.
- **Restaurar un `staticmethod` monkeyparcheado exige re-envolverlo.** Leer
  `Cls.attr` devuelve la función plana, no el descriptor; reasignarla pelada la
  convierte en método de instancia y cada `self.attr()` siguiente le pasa `self`.
  Correcto: `Cls.attr = staticmethod(original)`. Un error así **aborta el runner** y
  el conteo de checks cae (85 → 61) sin que sea obvio por qué.
- **`calls` mezclaba tuples de 2 y de 3.** Filtrar por `entry[0]` antes de
  desempaquetar, no asumir una forma única.
- **Fakes incompletos.** `_FakeNode` no tenía `state`; la nueva comprobación lanzaba
  `AttributeError` que un `except Exception` se tragaba, y entonces ni `commit` ni
  `push` se ejecutaban. Corregir el fake, **no** meter un `getattr` defensivo en
  producción para un caso imposible.
- **No comparar con deepdiff lo que se está probando de deepdiff.** Es circular: un
  deepdiff ciego al cambio reporta los datos como ya correctos. Usar `repr()`.
- **Un test que codifica comportamiento viejo hay que actualizarlo, no borrarlo.**
  `stray update without start changes nothing` protegía contra overlays fantasma; se
  reescribió a `an update after end does not resurrect the overlay` y se agregó
  `an update with no start still opens the overlay` para el caso del viewer tardío.
  Los dos juntos fijan la discriminación.
- **Si un test falla por un cambio, primero preguntarse si el test tiene razón.**
  `curves we created are removed on confirm` y `confirm leaves no orphan keyframe`
  la tenían: mi atajo de conservar el overlay en un confirm dejaba un keyframe
  fantasma que el siguiente overlay respaldaba como `original`, causando deriva.

### 5.4 Instrumentación

- Ponerla **donde el código realmente pasa** (ver 4.7).
- Throttlear todo log en camino caliente. Un diagnóstico sin throttle en
  `on_scene_update` produjo **2904 líneas** en una sesión.
- **Quitar el diagnóstico cuando ya respondió su pregunta.** `_diagnose_no_transform()`
  se eliminó porque con streaming por selección su condición de disparo pasó a ser
  el estado idle normal: habría logueado cada segundo.
- Los caminos silenciosos son el enemigo. `interactive_transform_active()` tragaba
  `AttributeError` y devolvía `None` — indistinguible de "no hay transform".
  `LiveSyncEngine.tick()` tragaba todo con `logging.debug`, que a nivel INFO **no
  llega al archivo**. Ambos se subieron a WARNING con throttle.
- Nombrar el valor, no solo el hecho: `Animation dep <uuid> is stranded in state N`
  en vez de confiar en el `Commit skipped` de porcelain, que no nombra el estado.

### 5.5 Cómo verificar

```
"<blender.exe>" --background --python tests\test_live_sync.py
```

**Versión objetivo: el portable 5.2.2 LTS.** Es el único Blender donde el usuario
prueba el addon, así que es la versión que debe funcionar. Correr también 4.5 y 5.1
es útil como red (`blender_manifest.toml` declara `blender_version_min = "4.5.0"` sin
cota superior), pero **no es obligatorio**: si soportar una versión anterior obliga a
complicar el código o introduce riesgo en 5.2.2, se deja esa versión. Decisión del
usuario, 2026-10-19.

En la práctica esto ya se aplicó: el podado de curvas de 4.3 cubre solo la rama de
Blender 5, y la rama 4.x se dejó como estaba en vez de reescribirla.

Filtrar salida con `findstr /C:"checks passed" /C:"RESULT:" /C:"FAIL"`.

En 5.2 un `RESULT: PASS` puede imprimir **después** del veredicto, y con exit code
0, un traceback de teardown:
`RuntimeError: unregister_class(...):, missing bl_rna attribute from '_RNAMeta' instance`.
Es ruido del harness, no un fallo.

`--background` no tiene ventana, así que `wm.operators` está siempre vacío: la
detección de transforms modales **no se puede probar headless**.

### 5.6 Logs de sesión

```
portable\extensions\.user\user_default\multi_user\cache\multiuser_YYYY_MM_DD_HH-MM-SS.log
```

Uno por peer y por sesión, escritos por `setup_logging()` (`operators.py`) con
`mode='w'`. Se crean solos al iniciar o unirse; no hay que pedirle nada al usuario.

---

## 6. Problemas encontrados y cómo se resolvieron

Formato de cada entrada: **síntoma** → causa raíz → arreglo → qué lo fija.

### 6.1 Los toggles de commit/push desaparecieron del panel

**Síntoma:** "quitaste el feature que había puesto sobre los toggles".
**Causa:** se gateó la UI por `runtime_settings.is_host` — master toggle sin dibujar,
`SessionToggleLiveSync.poll` devolviendo `False`, `live_sync_nodes.clear()` para
no-Hosts. Como `is_host` solo lo ponía `SessionInitOperator`, los controles
desaparecían casi siempre.
**Arreglo:** revertido por completo. `poll` devuelve `True` y los controles se
dibujan sin condición. `is_host` es legítimo para el rol de transporte y para
`can_publish`, **nunca** para visibilidad de panel.
**Lo fija:** `test_toggle_draw_rules`, que itera `for is_host in (True, False)`.

### 6.2 Los toggles armados se comportaban como botones

Dos causas **independientes**; arreglar una no arreglaba la otra.

**(a) Marcador `applied_updates` envenenado.**
**Causa:** `DynamicRightSelectTimer` marcaba el uuid tras cambiar `obj.hide_select`,
que no está en ningún filtro de `BlObject.dump`. Al no replicarse, nadie consumía el
marcador, y el siguiente edit genuino del usuario se descartaba como eco remoto.
**Arreglo:** eliminar el marcador; conservar `update_user_metadata`, que es de lo que
vive el overlay de presencia.
**Lo fija:** el invariante documentado en 4.1.

**(b) Nodo varado fuera de `ADDED`/`UP`.**
**Causa:** `porcelain.commit` rechaza cualquier otro estado, y `push` solo lo
devuelve a `UP`. Un push interrumpido dejaba el nodo inutilizable para el resto de la
sesión; solo el toggle manual lo destrababa, porque hace `push(force=True)`.
**Arreglo:** `force=stranded` con `stranded = dep.state not in (ADDED, UP, COMMITED)`,
más un warning que nombra el estado. Forzar incondicionalmente **no** sirve: envía el
nodo completo y hace que cada viewer reconstruya el Action (ver 6.12).
**Lo fija:** `test_animation_dependency_push`, que incluye un nodo en `COMMITED` y uno
en `FETCHED` y verifica cuál se fuerza.

### 6.3 No había stream en vivo: todos los peers salían VIEWER

**Causa:** `is_host = True` solo se asignaba en `SessionInitOperator`
(`wm.session_init`), pero el botón que hospeda es `SessionHostOperator`
(`wm.session_host`), que llamaba a `session.host()` sin tocar la propiedad. Sin Host
no hay broadcaster.
**Arreglo:** asignarla tras un `session.host()` exitoso, antes de `setup_timer()`.
**Confirmado en log real:** `Live Sync: session endpoint 127.0.0.1:5556 (role=HOST broadcaster)`.

### 6.4 El Host era broadcaster pero nunca streameaba

**Síntoma:** 4+ minutos de sesión con cero `start on N bone(s)` y cero motivos de skip.
**Causa:** dos. (1) `TRANSFORM_OPERATORS` no incluía `TRANSFORM_OT_trackball`, que es
lo que corre la tecla R. (2) Se escaneaba todo `wm.operators`, que es un histórico
acumulativo.
**Cómo se diagnosticó:** `_diagnose_no_transform()` volcaba el stack crudo una vez por
segundo. Ahí apareció `TRANSFORM_OT_trackball`. **El diagnóstico se eliminó después**,
porque con streaming por selección su condición de disparo pasó a ser el estado idle
normal.
**Arreglo:** agregar `transform.trackball`, inspeccionar solo `operators[-1]`,
expiración por movimiento real (`_motion_stalled`), y separar `pose_stream_active()`
del estado del streamer.
**Lo fija:** `a selected-but-not-dragged bone does not suppress porcelain`.

### 6.5 La sesión moría en el mismo segundo en que arrancaba

**Síntoma en log:** `'NoneType' object has no attribute 'identifier'` seguido de
`Exiting session, reason Error during timer SessionListenTimer execution`.
**Causa:** `dump_animation_data` leía `action_slot.identifier` sin comprobar `None`. En
Blender 5 asignar `animation_data.action` **no** pobla `action_slot`. El
`AttributeError` escapaba de `execute()` y `Timer.main()` lo convertía en
`session.disconnect()`.
**Arreglo:** guard de `None` en el dump. Ídem para `channelbag is None` en
`BlAction.dump`.
**Lección:** ver 3.3 — cualquier excepción en un timer mata la sesión.

### 6.6 El hueso del cliente sin keyframes, y el cubo desconectado del hueso

**Causa:** `load_animation_data` hacía `action.slots[identifier]` sin guardar. El raise
abortaba el apply. En `BlObject.load` eso es especialmente dañino porque
`load_animation_data` es la **primera** sentencia y el bloque `# Parenting` va ~35
líneas después: el objeto se quedaba sin padre.
**Arreglo:** `slots.get()` + fallback al slot único cuando el identifier no sobrevivió
al rebuild + `try/except` que loguea en vez de lanzar.
**Nota:** el identifier **nunca** sobrevive un round trip, porque `slots.new()` genera
uno nuevo. Por eso el fallback es necesario, no defensivo.

### 6.7 Los keyframes del cliente se borraban al commitear en el Host

**Causa:** `BlAction.load` borraba **todos** los slots antes de saber si tenía datos
para reconstruir. Un payload sin curvas dejaba el Action en cero.
**Arreglo:** en dos pasos. Primero un guard por conteo de **curvas**
(`count_action_curves`). Después, cuando se vio que un payload con slots pero con
`fcurves` vacías pasaba ese guard, se reforzó a conteo de **keyframes**
(`count_action_keyframes`) con warning `lost keyframes on reload`.
**Pendiente:** ese warning cuenta los keyframes del overlay como reales (ver 7.3).

### 6.8 La rotación en quaternion no sincronizaba

**Causa:** `_rotation_data_path` devolvía `"rotation_quat"`. Esa propiedad **no existe**
en `PoseBone`; el nombre real es `rotation_quaternion`. Como un `data_path` de F-Curve
es una ruta Python real, se creaban curvas que nada evaluaba.
**Cómo se descubrió:** al cambiar a canales crudos, un test reventó con
`AttributeError: 'PoseBone' object has no attribute 'rotation_quat'`. En vez de
adivinar el nombre, se sondeó `PoseBone.bl_rna.properties` en 4.5.2 y 5.2.2 —
resultado idéntico.
**Arreglo:** `rotation_quaternion` en los tres sitios (`_rotation_data_path`,
`_bone_rotation_values`, `_set_bone_transform`).

### 6.9 Rotaciones mayores a 180° llegaban normalizadas

**Síntoma:** Host 270° → viewer -90°. Misma dirección, valor interno distinto,
reproducción incorrecta.
**Causa:** se transportaba `matrix_basis`, y una matriz no puede representar más de una
vuelta completa. La información se perdía **antes** de salir del Host.
**Arreglo:** transportar canales crudos (`location`, `rotation_mode`, `rotation`,
`scale`) y comparar el payload —no la matriz— para detectar movimiento, porque 270° y
630° comparten matriz.
**Lo fija:** `test_multi_turn_rotation_is_preserved`.

### 6.10 El keyframe temporal solo aparecía al transformar

**Causa:** el stream se gateaba por transform en ejecución, más estrecho que el spec
("la posición que tiene **seleccionada** el host").
**Arreglo:** `_stream_wanted()` = armature trackeado en POSE con hueso seleccionado.

### 6.11 Un viewer que conectaba tarde nunca veía el overlay

**Síntoma en log:** ~40 s de `viewer applied 1/N [update=N]` sin ninguna línea `keying`.
**Causa:** `start` se emite **una vez por selección** y el stream queda abierto. Un
viewer que conecta después no lo recibe, y `_on_update` descartaba todo en silencio.
**Arreglo:** dos piezas, hacen falta ambas. `_revive_overlay()` reconstruye desde un
`update` (rechazado si la clave está en `_ended`), y `HEARTBEAT_INTERVAL` reenvía la
pose en reposo.
**Lo fija:** `an update with no start still opens the overlay` y
`an update after end does not resurrect the overlay`.

### 6.12 El overlay se cortaba cada segundo durante un arrastre

**Síntoma en log:** `[end=1, start=1, update=1]` cada segundo; el hueso del viewer
"daba saltos o se quedaba quieto".
**Causa:** regresión propia. `_last_motion` solo se actualizaba cuando una matriz
**cambiaba**, así que pausar el mouse un segundo disparaba `_motion_stalled()` →
`_finish()`. Como el arrastre aún no se confirmaba en `bpy.data`, `_finish` lo leía
como **cancelación** y devolvía el hueso a su pose original.
**Arreglo:** la ociosidad sola ya no termina el stream; se exige además que la pose
haya **vuelto** al snapshot. Un arrastre real mantiene la pose evaluada lejos del
snapshot aunque el mouse esté quieto.
**Lo fija:** `a heartbeat does not count as motion`.

### 6.13 El viewer no recibía nada hasta que el Host movía algo

**Causa:** combinacion de 6.11 — el `start` único se emitió con `0 viewer(s)
connected`, y `_update` calla mientras la pose no cambia.
**Arreglo:** detectar cambio en `broadcaster.client_count` y reenviar de inmediato
(`force=True, heartbeat=True`), más `HEARTBEAT_INTERVAL` de 0.25 a 0.1 como red de
seguridad.
**Lo fija:** `test_heartbeat_resends_unchanged_pose`.

### 6.14 El Cliente pusheaba y el Host destruía objetos

**Síntoma en log del Host:** cientos de
`Post view widget exception: StructRNA of type Object has been removed`. En el log del
Cliente: `committed Collection <uuid> (state 4 -> 1)`.
**Causa:** `bl_check_common = True` en Collection/Scene/World dejaba pasar a cualquier
peer, y porcelain permite COMMON a todos. El Host aplicaba esas Collections,
`BlCollection.load` desarmaba y rearmaba objetos, y el armature perdía el enlace a su
Action.
**Arreglo:** `can_publish` rechaza a cualquier peer no-Host **antes** de mirar
propiedad o `check_common`.
**Lo fija:** `test_can_publish_common_nodes`, 8 checks.
**Validado en log:** los `StructRNA ... has been removed` desaparecieron por completo.

### 6.15 El overlay restauraba valores viejos sobre datos recién aplicados

**Causa:** `BlAction.load` reconstruye el Action, así que los wrappers de fcurve y los
valores respaldados del overlay quedan muertos. Restaurar después escribía valores
viejos sobre las curvas nuevas.
**Arreglo:** `invalidate_overlays()` descarta el bookkeeping **sin restaurar**, llamado
desde `ApplyTimer` tras cualquier apply que recargue un Action (incluidos los hijos
forzados por `bl_reload_child`). `cleanup()` no sirve ahí precisamente porque restaura.
**Lo fija:** `test_invalidate_overlays_does_not_restore`.
**Nota:** se intentó primero el atajo de conservar el overlay en un confirm honrando
`is_cancelled`. Dos tests lo rechazaron con razón: dejaba un keyframe fantasma que el
siguiente overlay respaldaba como `original`, causando deriva acumulativa.

### 6.16 El hueso del viewer se congelaba y los keyframes parpadeaban

**Síntoma en log:** el mismo Action `6ba3e217` re-aplicado ~6 veces por segundo,
siempre `lost keyframes on reload: 21 -> 12`, con **cero** commits en el Host.
**Causa:** el viewer re-aplicaba datos idénticos en un ciclo. Cada pasada demolió los
slots y los reconstruyó, dejando el armature sin animación evaluable entre medias.
**Arreglo:** dos piezas. (1) Guarda de **identidad** en `ApplyTimer`: se recuerda el
objeto `data` aplicado, y si el nodo vuelve a FETCHED con el mismo objeto se asienta a
`UP`. Es fiable porque `do_commit` siempre reemplaza `node.data` por un objeto nuevo;
se guarda la referencia, no un `id()`, para que no engañe una dirección reciclada.
(2) Recarga **incremental**: reutilizar slots emparejados por
`(target_id_type, name_display)` y descartar solo los que el payload ya no trae.
**Sin test dedicado** — ver 7.2.
**Causa raíz del ciclo, sin identificar:** qué re-marca el nodo como FETCHED sin
commits del Host. Ver 7.1.

### 6.17 El keyframe temporal quedaba "confirmado" en el viewer

**Causa:** consecuencia directa de la recarga incremental de 6.16. `load_fcurve` limpia
los puntos, pero solo de las curvas presentes en el dump. Las curvas que el overlay
**inventó** (location y scale cuando el Host solo keyeó rotación: payload de 3 curvas
contra 9 canales escritos) sobrevivían, y `invalidate_overlays` descartaba el
bookkeeping sin restaurar → keyframe huérfano permanente.
**Arreglo:** podar del channelbag toda curva que el dump no trae. El dump es la verdad
completa de su slot.
**Sin test dedicado** — ver 7.2.
**Lección:** un arreglo puede crear el siguiente bug. La demolición de slots era
brutal pero **limpiaba** las curvas inventadas; al hacerla incremental hubo que
reponer esa limpieza explícitamente.

---

## 7. Cabos sueltos conocidos

1. **Qué re-marca un nodo como FETCHED** sin que el Host commitee. Un log real
   mostró el mismo Action re-aplicado ~6 veces por segundo con cero commits en el
   Host. La guarda de identidad en `ApplyTimer` lo neutraliza, pero la causa raíz
   sigue sin identificarse y podría aparecer en otro tipo de nodo.
2. **Tres caminos sin test dedicado:** recargar un Action dos veces con un overlay en
   vuelo, la guarda de identidad FETCHED, y el podado de curvas. Es la deuda más
   concreta: pueden regresionar en silencio.
3. **`lost keyframes on reload`** cuenta los keyframes del overlay como reales, así
   que dispara en cada apply mientras haya overlay activo. Debería comparar contra el
   conteo de keyframes **del payload**.
4. **`presence.py`** lanza `Post view widget exception: StructRNA of type Object has
   been removed` cuando un widget sostiene referencias a objetos ya destruidos.
   Debería validarlas por sí solo.
5. **La rama 4.x de `BlAction.load`** no tiene podado de curvas y arrastra el quirk
   de iterar `data["fcurves"]` una vez por slot. **Aceptado y sin prioridad**: la
   versión objetivo es 5.2.2 (ver 5.5). Solo tocarla si aparece un requerimiento real
   de 4.x, y nunca a costa de complicar la rama de Blender 5.
6. **Rotación con `rotation_mode` distinto entre peers** pierde el conteo de vueltas
   (la conversión pasa por quaternion). Sin impacto mientras ambos usen el mismo modo.
