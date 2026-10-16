# Documento de Especificación Técnica

## Modificación del Addon "Multi-User" para Blender

## Sistema de Live Sync para Animación con Visor en Bucle

### 1. INFORMACIÓN GENERAL DEL PROYECTO

#### 1.1 Addon Base

- Nombre: Multi-User
- Repositorio: https://gitlab.com/slumber/multi-user
- Extensión Blender: https://extensions.blender.org/add-ons/multi-user/
- Documentación: https://slumber.gitlab.io/multi-user/index.html
- ReadTheDocs: https://multi-user.readthedocs.io/
- Autor original: Swann Martinez (Slumber)
- Estado del addon: En desarrollo activo. El propio repositorio indica: "Under development, use it at your own risks. Currently tested on Windows platform."
- Arquitectura: Cliente/Servidor con replicación orientada a datos (data-oriented replication). Usa ZeroMQ (ZMQ) para comunicación de red. El sistema de sincronización se basa en un repositorio local con operaciones tipo Git (commit, push, apply, lock, unlock, rm) a través de una capa de abstracción llamada porcelain.
- Permisos requeridos: El addon solicita permiso de Red (Network) para sincronizar la escena.

#### 1.2 Objetivo de la Modificación

Modificar el addon Multi-User para habilitar un flujo de trabajo de animación en tiempo real donde:

- El Host actúa como editor/animador principal.
- El Cliente actúa como un visor/reviewer que reproduce la animación en bucle.
- El Host puede editar poses interactivamente (R, G, S en Modo Pose) y el Cliente ve esos cambios en vivo, integrados dentro de su reproducción en bucle como un keyframe temporal en el frame específico de edición.
- El timeline del Cliente nunca se detiene, pausa ni altera.

#### 1.3 Motivación

El caso de uso principal es la `revisión de animación`. El animador (Host) trabaja en una pose específica mientras el revisor (Cliente) observa la animación completa corriendo en bucle y ve, en contexto, cómo se ve la edición del Host en el frame específico que está siendo trabajado. Esto permite evaluar las transiciones de entrada y salida de la pose editada dentro del flujo completo de la animación.

#### 1.4 Contexto de Permisos en el Addon Original

En el addon vanilla, el sistema de permisos funciona mediante propiedad y bloqueo de data blocks. Cada data block (objeto, malla, material, colección, etc.) tiene un propietario (owner). Solo el propietario o un data block común (RP_COMMON) puede ser modificado. No existe un modo global de "solo lectura" para clientes. La modificación que estamos implementando no altera este sistema de permisos; el Host sigue siendo el propietario de los data blocks y el Cliente actúa como receptor pasivo de los cambios.

### 2. ESCENARIO DE TRABAJO Y CONFIGURACIÓN

#### 2.1 Configuración de Archivos

| Rol             | Archivo                                       | Comportamiento del Timeline                                                                                                 |
| --------------- | --------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------- |
| Host (Editor)   | Abre el .blend original                       | Libre. Puede pausar, hacer scrub, ir a cualquier frame, editar donde quiera. No está en bucle.                              |
| Cliente (Visor) | Abre una copia del .blend hecha por el usario | Se usara como reproductor, reproduciendo en bucle constantemente. No se pausa, no se detiene, no es controlado por el Host. |

- Son 2 instancias separadas de Blender, cada una con su propio archivo y su propio timeline independiente.
- El Host tiene control libre de su timeline.
- El Cliente SÍ está en bucle (controlado por el usuario dentro de Blender, el addon no lo controla). El Cliente es el que reproduce la animación continuamente.
- El addon sincroniza la memoria (`bpy.data`), no el archivo en disco. Cuando el Cliente se conecta, el addon descarga los data blocks del servidor y los construye en su escena local mediante porcelain.construct() y porcelain.apply().
- No se debe abrir el mismo archivo físico desde una carpeta compartida de red. Blender bloquea archivos .blend abiertos y puede causar corrupción.

#### 2.2 Flujo de Conexión

1. Host: Abre su .blend original. Inicia el servidor Multi-User desde el panel del addon.
2. Cliente: Abre la copia del .blend. En el panel Multi-User, introduce la IP del Host y se conecta.
3. Al conectarse: El addon en el Cliente descarga todos los data blocks del servidor, construye la escena local, y asigna los mismos UUIDs que en el Host para mantener la coherencia de rastreo.

### 3. RESTRICCIONES FUNDAMENTALES

#### 3.1 NO PAUSAR EL CLIENTE

- Bajo ninguna circunstancia el sistema debe pausar, detener, ni alterar el timeline del Cliente.
- El Cliente reproduce su bucle de animación de forma continua e ininterrumpida.
- Toda la lógica de sincronización debe funcionar alrededor del bucle del Cliente, no en contra de él.
- No se debe ejecutar bpy.context.screen.is_animation_playing = False en el Cliente como parte del flujo de Live Sync.
- No se debe ejecutar bpy.context.scene.frame_set() en el Cliente para forzar un frame específico como parte del flujo de Live Sync.

#### 3.2 NO SILENCIAR EL HUESO

- El hueso que el Host está editando NO debe ser silenciado. No se debe usar fcurve.mute = True sobre las curvas F del hueso editado.
- El hueso que el Host está editando NO debe ser congelado ni desconectado de la animación.
- El hueso debe continuar animándose normalmente dentro del bucle del Cliente.
- La edición del Host se manifiesta como un keyframe temporal insertado o sobrepuesto en el frame específico donde el Host está trabajando.

#### 3.3 NO USAR ACTION SWAPPING

- NO se debe crear una Acción vacía temporal ni intercambiar Acciones (animation_data.action).
- NO se debe reemplazar la Acción original del Cliente por otra Acción.
- NO se debe desconectar la Acción original del armazón durante la edición interactiva.
- Los keyframes temporales se insertan o sobreescriben dentro de la Acción existente del Cliente.

#### 3.4 NO CREAR KEYFRAMES MASIVOS

- NO se deben crear 60 keyframes por segundo ni keyframes en cada frame del bucle.
- Solo se manipula UN keyframe temporal por hueso editado, ubicado en el frame específico del Host.
- Ese keyframe se actualiza (no se recrea) cada vez que el Host mueve el hueso durante la misma transformación interactiva.

#### 3.5 MODIFICACIÓN TEMPORAL DE LA ANIMACIÓN DEL CLIENTE

- La animación del Cliente es modificada temporalmente durante la edición interactiva del Host. Esto es necesario, esperado y aceptable.
- Se insertan o sobreescriben keyframes en el frame específico donde el Host está trabajando.
- Los valores originales de los keyframes afectados se guardan en memoria antes de cualquier sobreescritura.
- Si el Host cancela la edición, los valores originales se restauran completamente, devolviendo la animación a su estado previo a la edición interactiva.
- Si el Host confirma la edición, el commit/push real del repositorio actualiza los datos de la animación. Los datos temporales son reemplazados por los datos confirmados.
- Ninguna modificación temporal debe sobrevivir a la cancelación de la edición ni convertirse en permanente fuera del flujo de commit/push del repositorio.

#### 3.6 RENDIMIENTO

- NO usar bone.keyframe_insert() estándar de Blender. Es demasiado lento para operaciones en tiempo real porque recalcula tangentes, actualiza el depsgraph y dispara handlers internos.
- SÍ usar fcurve.keyframe_points.insert(frame, value, options={'FAST'}) para inserción rápida de keyframes.
- El stream de datos de transformación en vivo debe usar un canal ZMQ directo, evitando pasar por porcelain.commit() y porcelain.push() que son operaciones pesadas de repositorio y generan historial de commits.

#### 3.7 THREADING

- NO usar hilos de Python (threading) para leer/escribir datos de `bpy.data`. Blender no es thread-safe para datos de escena y el acceso desde hilos secundarios causa crashes (Segmentation Fault).
- SÍ usar bpy.app.timers o handlers nativos de Blender (depsgraph_update_post, frame_change_post, etc.) para tareas periódicas o reactivas.

### 4. FUNCIONALIDAD 1: TOGGLE GLOBAL DE COMMIT/PUSH CONSTANTE

#### 4.1 Comportamiento Actual del Addon (Vanilla)

En el panel Repository del addon (pestaña Multiuser en la barra lateral N de la Vista 3D), cada data block (objeto, material, malla, colección, escena, etc.) tiene un botón individual de commit/push (wm.session_datablock_commit).

Al presionar ese botón, se ejecuta:

```python
porcelain.commit(session.repository, target_uuid)
porcelain.push(session.repository, 'origin', target_uuid, force=True)
```

Esto envía manualmente ese data block específico al servidor. Es una operación de una sola vez.

#### 4.2 Cómo funciona el depsgraph y el handler actual

Blender tiene un sistema llamado Dependency Graph (Depsgraph). Es un registro interno que rastrea todas las relaciones entre los datos de la escena. Cada vez que algo cambia en la escena (mueves un objeto, cambias un material, editas una malla, modificas un hueso, etc.), el depsgraph registra ese cambio y dispara una señal llamada depsgraph_update_post.

El addon Multi-User ya tiene un handler que escucha esta señal. Está en el archivo handlers.py y se llama on_scene_update:

```python
@persistent
def on_scene_update(scene):
    """Forward blender depsgraph update to replication"""
    if session and session.state == STATE_ACTIVE:
        blender_depsgraph = bpy.context.view_layer.depsgraph
        dependency_updates = [u for u in blender_depsgraph.updates]

        for update in reversed(dependency_updates):
            update_uuid = getattr(update.id.original, 'uuid', None)
            if update_uuid:
                node = session.repository.graph.get(update_uuid)
                check_common = session.repository.rdp.get_implementation(update.id).bl_check_common
                if node and (node.owner == session.repository.username or check_common):
                    if node.state == UP:
                        porcelain.commit(session.repository, node.uuid)
                        porcelain.push(session.repository, 'origin', node.uuid)
```

Cada vez que cambia algo en la escena, esta función se ejecuta y recibe una lista de todos los data blocks que fueron modificados. Luego verifica condiciones de ownership y estado antes de hacer commit/push.

#### 4.3 Qué detecta y qué NO detecta el depsgraph

Esto es fundamental para entender el alcance de la Funcionalidad 1:

| Acción del usuario                                                | ¿Se dispara depsgraph_update_post? | ¿La Funcionalidad 1 lo detecta? |
| ----------------------------------------------------------------- | ---------------------------------- | ------------------------------- |
| Mover un objeto en el viewport y soltar (confirmar)               | ✅ Sí                              | ✅ Sí                           |
| Cambiar una propiedad en el panel de propiedades                  | ✅ Sí                              | ✅ Sí                           |
| Cambiar un material (color, rugosidad, etc.)                      | ✅ Sí                              | ✅ Sí                           |
| Añadir o eliminar un keyframe                                     | ✅ Sí                              | ✅ Sí                           |
| Presionar R/G/S y mientras estás arrastrando (antes de confirmar) | ❌ No                              | ❌ No                           |
| Editar una malla en Edit Mode y confirmar                         | ✅ Sí                              | ✅ Sí                           |
| Presionar ESC para cancelar una transformación                    | ❌ No (no hubo cambio)             | ❌ No                           |

Conclusión crítica: La Funcionalidad 1, basada en el depsgraph, es completamente ciega a las transformaciones interactivas (R, G, S) antes de que el usuario confirme la operación. Esto no es un error ni una limitación de nuestra modificación; es el comportamiento nativo de Blender. Las transformaciones interactivas son cubiertas por la Funcionalidad 2 (ver Sección 5).

#### 4.4 Comportamiento Deseado con la Modificación

Se añade un Toggle Global en la interfaz del addon.

**Cuando el Toggle Global está DESACTIVADO:**

- Todo funciona exactamente igual que el addon vanilla.
- Los botones individuales de commit/push siguen siendo botones normales de una sola acción.
- Al presionar un botón, se hace un commit/push manual de una sola vez para ese data block.

**Cuando el Toggle Global está ACTIVADO:**

- Los botones individuales de commit/push se convierten en toggles (botones presionables con estado visual depress=True/False).
- Cada toggle individual controla si ese data block específico hace commit/push constantemente. "Constantemente" significa: cada vez que el depsgraph detecta un cambio en ese data block, se ejecuta commit/push automáticamente. Si no hay cambios, no se envía nada. Si hay 10 cambios en un segundo, se hacen 10 commit/push.
- Al activar un toggle individual:
  - Se hace un commit/push inmediato de ese data block.
  - Se registra ese data block en un diccionario de seguimiento (shared_data.session.live_sync_nodes) para commit/push automático en cada cambio posterior detectado por el depsgraph.
- Al desactivar un toggle individual:
  - Se detiene el commit/push automático para ese data block.
  - El data block se elimina del diccionario de seguimiento.
  - Vuelve a requerir commit/push manual.
- Al desactivar el Toggle Global:
  - Todos los toggles individuales se desactivan.
  - Se limpia el diccionario de seguimiento.
  - Todos los data blocks dejan de hacer commit/push constante.
  - Los botones vuelven a ser botones normales de commit/push manual de una sola vez.

#### 4.5 Archivos a Modificar para esta Funcionalidad

| Archivo                     | Modificación                                                                                                             |
| --------------------------- | ------------------------------------------------------------------------------------------------------------------------ |
| ui.py                       | Modificar la función draw_property() para dibujar toggles o botones según el estado del Toggle Global.                   |
| operators.py                | Crear el operador SessionToggleLiveSync (wm.session_toggle_live_sync).                                                   |
| handlers.py                 | Modificar on_scene_update()" para que ejecute commit/push automático en los data blocks registrados como ""live sync""." |
| **init**.py o properties.py | Registrar la propiedad booleana multiuser_global_live_sync en el WindowManager.                                          |

#### 4.6 Estructura de Datos para el Estado de Live Sync

Se necesita un diccionario almacenado en la sesión compartida (shared_data.session) que rastree qué nodos tienen live sync activo:

```python
# Inicialización (en shared_data.session)
shared_data.session.live_sync_nodes = {}

# Ejemplo de estado con nodos activos
shared_data.session.live_sync_nodes = {
    "uuid-del-nodo-1": True,
    "uuid-del-nodo-2": False,
}
```

#### 4.7 Lógica del Handler Modificado (on_scene_update)

Dentro del handler on_scene_update en handlers.py, se debe añadir una condición adicional:

```python
if node:
    is_live = shared_data.session.live_sync_nodes.get(node.uuid, False)

    if is_live:
        # Este nodo tiene live sync activo.
        # Forzar commit/push sin importar otras condiciones de ownership.
        try:
            porcelain.commit(session.repository, node.uuid)
            porcelain.push(session.repository, 'origin', node.uuid)
        except Exception as e:
            logging.error(f"Live Sync Push Error: {e}")
    elif node.owner == session.repository.username or check_common:
        # Comportamiento vanilla normal
        if node.state == UP:
            porcelain.commit(session.repository, node.uuid)
            porcelain.push(session.repository, 'origin', node.uuid)
```

#### 4.8 Lógica de la Interfaz (draw_property en ui.py)

```
SI toggle_global_activo:
    dibujar operador "wm.session_toggle_live_sync" como toggle (depress=True/False)
    el estado visual del toggle refleja si ese nodo está en live_sync_nodes
SI NO:
    dibujar operador "wm.session_datablock_commit" como botón normal (comportamiento vanilla)
```

#### 4.9 Limitación Explícita de la Funcionalidad 1

La Funcionalidad 1 solo detecta cambios confirmados por el usuario, es decir, cambios que ya fueron escritos en `bpy.data`. Esto incluye:

- Mover un objeto y soltar.
- Cambiar una propiedad.
- Confirmar una transformación con click izquierdo o Enter.
- Añadir o eliminar keyframes.

La Funcionalidad 1 NO detecta cambios en curso que aún no han sido confirmados. Esto incluye:

- Estar rotando un hueso con R (sin haber hecho click aún).
- Estar moviendo un objeto con G (sin haber hecho click aún).
- Estar escalando con S (sin haber hecho click aún).

Estos cambios en curso son responsabilidad exclusiva de la Funcionalidad 2 (Live Sync de Modo Pose), descrita en la Sección 5.

### 5. FUNCIONALIDAD 2: LIVE SYNC DE MODO POSE (Transformaciones Interactivas)

#### 5.1 El Problema

En Blender vanilla, cuando estás en Modo Pose y presionas R (rotar), G (mover) o S (escalar) sobre un hueso:

- El hueso muestra la transformación visualmente en el viewport.
- Blender actualiza la representación visual interna (el depsgraph evaluado).
- Pero Blender NO escribe el cambio en `bpy.data` hasta que confirmas la operación (click izquierdo o Enter).
- El handler depsgraph_update_post NO se dispara durante la transformación interactiva.
- Por lo tanto, el addon Multi-User no detecta el cambio y no hace commit/push.
- El Cliente no ve la transformación hasta que el Host confirma.

#### 5.2 La Solución: Stream ZMQ Directo + Keyframe Temporal en el Frame del Host

Se implementa un canal de comunicación paralelo al sistema de repositorio (porcelain) del addon. Este canal envía datos de transformación en tiempo real directamente por ZMQ, sin pasar por el sistema de commits.

En el Cliente, la transformación recibida se manifiesta como un keyframe temporal insertado o sobrepuesto en el frame específico donde el Host está trabajando. El resto de la animación del Cliente sigue reproduciéndose normalmente. El hueso editado sigue animándose en todos los demás frames; solo en el frame específico del Host se muestra la pose editada.

#### 5.3 Flujo del Host (Editor)

**Paso 1: Inicio de la transformación**

1. El Host entra en Modo Pose y selecciona un hueso.
2. El Host presiona R, G o S para iniciar una transformación interactiva.
3. Se detecta el inicio de la transformación. Se envía por ZMQ una señal "start" con:
   - UUID del armazón
   - Nombre del hueso
   - Frame actual del Host (current_frame)

**Paso 2: Transformación en curso**

4. Mientras el Host mueve/rota/escala el hueso (sin confirmar aún):

- Se lee la pose visual usando evaluated_get(depsgraph) para obtener la matriz real del hueso tal como se ve en pantalla durante la transformación interactiva. (Ver Sección 5.9 para detalles técnicos.)
- Se envía por el canal ZMQ directo una señal "update" con:
  - UUID del armazón
  - Nombre del hueso
  - Frame actual del Host (current_frame)
  - Matriz de transformación (matrix_basis)
- Esto se repite continuamente mientras el Host mueve el mouse o las flechas del teclado.

**Paso 3a: Confirmación (click izquierdo o Enter)**

5. El Host confirma la transformación.

- Blender escribe el cambio en `bpy.data`.
- Se envía una señal "end" con is_cancelled = False.
- El depsgraph se dispara (ver Sección 6 para la coordinación con la Funcionalidad 1).

**Paso 3b: Cancelación (click derecho o ESC)**

5. El Host cancela la transformación.

- Blender NO escribe nada en `bpy.data`. Se descarta el cambio.
- Se envía una señal "end" con is_cancelled = True.
- El depsgraph NO se dispara.

#### 5.4 Flujo del Cliente (Visor)

**Paso 1: Recepción de la señal "start"**

1. El Cliente está reproduciendo su animación en bucle normalmente.
2. Recibe la señal "start".
3. Identifica el hueso y el frame del Host.
4. Guarda en memoria los valores originales del keyframe existente en ese frame (si existe). Esto es necesario para poder restaurar la animación si el Host cancela.
5. Prepara el estado para recibir actualizaciones.

**Paso 2: Recepción de señales "update"**

6. Recibe señales "update" continuamente mientras el Host edita.
7. Inserta o actualiza el keyframe temporal en el frame del Host con la nueva matriz recibida.

- Usa fcurve.keyframe_points.insert(frame, value, options={'FAST'}) para rendimiento.
- NO crea un keyframe nuevo cada vez. Si ya existe el keyframe temporal en ese frame, actualiza su valor.

8. La animación del Cliente sigue reproduciéndose. El keyframe temporal afecta únicamente el frame específico del Host.

**Paso 3a: Recepción de la señal "end" con confirmación**

9. Recibe la señal "end" con is_cancelled = False.
10. El keyframe temporal se mantiene momentáneamente.
11. El commit/push real por porcelain llega y actualiza la animación con los datos confirmados.
12. El keyframe temporal se limpia y es reemplazado por los datos reales del repositorio.
13. Se liberan los valores originales guardados en memoria.

#### 5.5 Resultado Visual en el Cliente

- El Cliente ve la animación corriendo en bucle normalmente.
- Todos los huesos se animan según la animación original.
- El hueso que el Host está editando también se anima normalmente en todos los frames.
- El hueso que el Host está editando también tiene el mismo comportamiento que vanilla.
- PERO, cuando el playhead del Cliente pasa por el frame específico donde el Host está trabajando, ese hueso muestra la pose editada por el Host en lugar de la pose original de ese frame.
- El reviewer puede ver cómo se ve la edición en contexto con las transiciones de entrada y salida de esa pose.

```
Cliente reproduce bucle:
Frame 1 → 2 → ... → 29 → [Frame 30: mano muestra la edición del Host] → 31 → ... → 100 → loop
                           ↑
                    Keyframe temporal insertado/sobrepuesto
                    con la pose que el Host está editando en vivo
```

#### 5.6 Detalle sobre la Modificación Temporal de la Animación del Cliente

La animación del Cliente sí se modifica temporalmente durante la edición interactiva. Esto es necesario y esperado. La modificación ocurre de la siguiente manera:

**Caso A: Ya existe un keyframe en el frame del Host (ej. frame 30)**

1. Se lee el valor actual del keyframe en el frame 30.
2. Se guarda ese valor en memoria como respaldo.
3. Se sobreescribe el keyframe en el frame 30 con el valor de la edición del Host.
4. La animación del Cliente queda temporalmente alterada en ese frame.

**Caso B: NO existe un keyframe en el frame del Host (ej. frame 30)**

1. Se inserta un keyframe nuevo en el frame 30 con el valor de la edición del Host.
2. La animación del Cliente queda temporalmente alterada (se añadió un dato que no existía).

**Restauración:**

- Si el Host cancela, en el Caso A se restaura el valor guardado. En el Caso B se elimina el keyframe insertado.
- Si el Host confirma, el commit/push del repositorio trae los datos definitivos que reemplazan cualquier estado temporal.

#### 5.7 Formato del Mensaje ZMQ (Canal Live Sync)

```json
{
  "channel": "live_sync",
  "action": "start | update | end",
  "armature_uuid": "uuid-del-armazon",
  "bone_name": "Nombre_Del_Hueso",
  "frame": 30,
  "matrix_basis": [
    [1, 0, 0, 0],
    [0, 1, 0, 0],
    [0, 0, 1, 0],
    [0, 0, 0, 1]
  ],
  "is_cancelled": false
}
```

- "action": "start" → Señal de inicio de transformación interactiva. Incluye frame y bone_name. No incluye matrix_basis.
- "action": "update" → Señal de actualización durante la transformación. Incluye frame, bone_name y matrix_basis.
- "action": "end" → Señal de fin de transformación. Incluye is_cancelled (true/false). Puede incluir frame y bone_name para identificación.

#### 5.8 Máquina de Estados del Live Sync

```
IDLE
  ↓ (Host inicia transformación R/G/S → señal "start")
INTERACTING
  ↓ (Host confirma o cancela → señal "end")
RESOLVING
  ↓ (Se limpia el keyframe temporal, se restauran valores, o se confirman datos)
IDLE
```

- IDLE: No hay transformación interactiva en curso. El sistema está en reposo.
- INTERACTING: El Host está editando un hueso. El Cliente está recibiendo señales "update" y actualizando el keyframe temporal.
- RESOLVING: El Host terminó la transformación (confirmó o canceló). El Cliente está limpiando el keyframe temporal y restaurando o confirmando datos.

#### 5.9 evaluated_get() Explicado

Durante una transformación interactiva (modal operator), `bpy.data` no se actualiza hasta confirmar. Para leer la transformación visual real, se debe usar el depsgraph evaluado:

```python
depsgraph = bpy.context.evaluated_depsgraph_get()
evaluated_bone = pose_bone.evaluated_get(depsgraph)
matrix = evaluated_bone.matrix_basis
```

- `evaluated_get(depsgraph)` devuelve una copia temporal y evaluada del objeto/hueso, tal como se ve en pantalla en ese momento.
- Esto incluye transformaciones interactivas no confirmadas, modificadores aplicados visualmente, y cualquier otro cambio que sea visible en el viewport pero que aún no se haya escrito en `bpy.data`.
- **Importante**: `porcelain.commit()` lee de `bpy.data` (los datos originales), no del depsgraph evaluado. Por eso las transformaciones interactivas deben ir por el canal ZMQ directo y no por `porcelain`.

#### 5.10 Archivos a Crear/Modificar para esta Funcionalidad

| Archivo                                     | Modificación                                                                                                                                                                              |
| ------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `libs/replication/live_sync.py`             | (NUEVO) Motor principal del Live Sync. Maneja el stream ZMQ, la inserción/actualización/restauración de keyframes temporales, el guardado de valores originales, y la máquina de estados. |
| `operators.py`                              | Crear operador modal o wrapper para detectar el inicio/fin de transformaciones interactivas (R, G, S).                                                                                    |
| `handlers.py`                               | Posiblemente añadir un handler para detectar el estado del modal de transformación.                                                                                                       |
| `libs/replication/session.py` o equivalente | Inyectar la escucha del canal "live_sync" en el socket ZMQ existente.                                                                                                                     |

### 6. INTERACCIÓN ENTRE FUNCIONALIDAD 1 Y FUNCIONALIDAD 2

#### 6.1 Principio General

Las dos funcionalidades operan de forma complementaria pero en fases distintas de la edición. La Funcionalidad 1 (Toggles + depsgraph) y la Funcionalidad 2 (Live Sync + evaluated_get) nunca actúan simultáneamente sobre la misma transformación durante la fase interactiva, sino que se alternan según el estado de la edición. La Funcionalidad 2 cubre la fase interactiva (antes de confirmar), y la Funcionalidad 1 cubre la fase post-confirmación.

#### 6.2 Fases de una Transformación Interactiva

**Fase 1: Inicio de la transformación interactiva**

- El Host presiona `R`, `G` o `S` sobre un hueso en Modo Pose.
- `depsgraph_update_post` NO se dispara. Blender aún no ha escrito en `bpy.data`.
- Funcionalidad 1: Inactiva. El depsgraph no detecta ningún cambio. Los toggles de Live Sync no tienen efecto en esta fase.
- Funcionalidad 2: Activa. Detecta el inicio de la transformación y envía la señal "start" por el canal ZMQ directo.

**Fase 2: Transformación en curso (antes de confirmar)**

- El Host está rotando/moviendo/escalando el hueso. El cambio es solo visual.
- `depsgraph_update_post` NO se dispara.
- **Funcionalidad 1**: Inactiva. No hay cambios en `bpy.data` que detectar. Los toggles de Live Sync no tienen efecto en esta fase.
- **Funcionalidad 2**: Activa. Lee `evaluated_get(depsgraph)` y envía señales `"update"` con la matriz visual del hueso por el canal ZMQ directo. El Cliente actualiza el keyframe temporal.

**Fase 3a: Confirmación (click izquierdo o Enter)**

- El Host confirma la transformación. Blender escribe el cambio en `bpy.data`.
- `depsgraph_update_post` SE DISPARA.
- **Funcionalidad 2**: Envía la señal `"end"` con `is_cancelled = False` por el canal ZMQ directo. El Cliente limpia el keyframe temporal.
- **Funcionalidad 1**: El depsgraph detecta el cambio. Si el toggle de Live Sync está activo para el armazón o el data block afectado, se ejecuta `porcelain.commit()` y `porcelain.push()` automáticamente. Si el toggle NO está activo, el cambio queda pendiente de commit manual.
- **Coordinación**: La señal `"end"` de la Funcionalidad 2 y el commit/push de la Funcionalidad 1 ocurren en paralelo. Ver Sección 6.3 para detalles.

**Fase 3b: Cancelación (click derecho o ESC)**

- El Host cancela la transformación. Blender NO escribe nada en `bpy.data`.
- `depsgraph_update_post` NO se dispara.
- **Funcionalidad 1**: Inactiva. No hay cambios en `bpy.data` que detectar. No se ejecuta commit/push.
- **Funcionalidad 2**: Envía la señal `"end"` con `is_cancelled = True` por el canal ZMQ directo. El Cliente restaura los valores originales del keyframe y elimina el keyframe temporal.

#### 6.3 Tabla Resumen de Actividad por Fase

| Fase                                   | ¿depsgraph se dispara? | Funcionalidad 1 (Toggles + depsgraph) | Funcionalidad 2 (Live Sync + evaluated_get)  |
| -------------------------------------- | ---------------------- | ------------------------------------- | -------------------------------------------- |
| Inicio de transformación (R/G/S)       | ❌ No                  | Inactiva                              | Activa: envía `"start"`                      |
| Transformación en curso (arrastrando)  | ❌ No                  | Inactiva                              | Activa: envía `"update"`                     |
| Confirmación (click izquierdo / Enter) | ✅ Sí                  | Activa: commit/push si toggle activo  | Activa: envía `"end"` (`is_cancelled=false`) |
| Cancelación (ESC / click derecho)      | ❌ No                  | Inactiva                              | Activa: envía `"end"` (`is_cancelled=true`)  |

#### 6.4 Coordinación en la Fase de Confirmación

En la fase de confirmación (Fase 3a), ambas funcionalidades actúan brevemente al mismo tiempo. Es importante que no haya conflicto entre ellas:

1. La Funcionalidad 2 envía la señal `"end"` por el canal ZMQ directo. Esto le dice al Cliente que la edición interactiva terminó y que debe limpiar el keyframe temporal.
2. La Funcionalidad 1 ejecuta `porcelain.commit()` y `porcelain.push()` (si el toggle está activo). Esto envía los datos confirmados por el canal del repositorio.
3. El Cliente debe procesar primero la señal `"end"` (limpiar el keyframe temporal) y luego aplicar los datos del repositorio (recibir la animación confirmada). Si el orden se invierte, el Cliente podría mostrar brevemente datos inconsistentes.

#### 6.5 Posible Vacío de Datos si el Toggle NO está Activo

Si el Host confirma la transformación pero el toggle de Live Sync NO está activo para el armazón o data block afectado:

- La Funcionalidad 2 envía la señal `"end"` y el Cliente limpia el keyframe temporal.
- La Funcionalidad 1 NO hace commit/push automático (porque el toggle no está activo).
- El cambio queda pendiente de commit manual en el Host.
- El Cliente ve que el keyframe temporal desaparece pero no recibe los datos confirmados hasta que el Host haga el commit manualmente.
- Esto puede causar un breve "vacío visual" donde el Cliente muestra la pose original del frame en lugar de la pose editada.

**Recomendación**: Para el flujo de trabajo de animación en tiempo real, se recomienda tener el toggle de Live Sync activo para el armazón y los data blocks relevantes. Esto asegura que la confirmación se propague inmediatamente al Cliente sin vacío de datos.

### 7. LO QUE ESTRICTAMENTE NO SE DEBE IMPLEMENTAR

1. NO pausar el timeline del Cliente. El Cliente siempre reproduce su propio timeline, normalmente en bucle. No ejecutar `is_animation_playing = False` ni `frame_set()` en el Cliente como parte del flujo de Live Sync.
2. NO silenciar (`mute`) las Curvas F del hueso editado. No usar `fcurve.mute = True`. El hueso debe seguir animándose.
3. NO congelar ni desconectar el hueso editado de la animación del Cliente.
4. NO usar Action Swapping. No crear Acciones vacías temporales. No intercambiar `animation_data.action`. No reemplazar la Acción original del Cliente.
5. NO crear keyframes en cada frame del bucle del Cliente. Solo se manipula UN keyframe temporal por hueso editado, ubicado en el frame específico del Host.
6. NO usar `bone.keyframe_insert()` estándar. Usar `fcurve.keyframe_points.insert()` con `options={'FAST'}`.
7. NO usar hilos de Python (`threading`) para acceder a datos de `bpy.data`. Usar `bpy.app.timers` o handlers nativos de Blender.
8. NO hacer commit/push por `porcelain` durante la transformación interactiva (antes de confirmar). Usar el canal ZMQ directo para las actualizaciones en vivo.
9. NO dejar keyframes temporales huérfanos. Toda modificación temporal debe ser limpiada ya sea por confirmación (commit/push real) o por cancelación (restauración de valores originales).

### 8. CONSIDERACIONES DE RENDIMIENTO

1. **Inserción de keyframes**: Usar siempre `options={'FAST'}` para evitar recálculo de tangentes y actualizaciones pesadas del depsgraph. Esto hace la inserción casi instantánea.
2. **Frecuencia de actualización**: El stream ZMQ debe enviar actualizaciones a una frecuencia razonable (30-60 FPS). No saturar la red ni el socket ZMQ.
3. **Filtrado de data blocks para el Toggle Global de Live Sync**: Cuando se hace commit/push constante, NO enviar data blocks pesados como `mesh`, `image`, `volume` en cada cambio. Priorizar data blocks ligeros y relevantes para animación (aunque en realidad el toggle es el quele permite al usuario elegir):

| Data Block                       | Peso         | ¿Conviene Live Sync? |
| -------------------------------- | ------------ | -------------------- |
| `object` (transformación)        | Ligero       | ✅ Sí                |
| `scene` (frame actual, settings) | Ligero       | ✅ Sí                |
| `camera`                         | Ligero       | ✅ Sí                |
| `armature` (huesos)              | Ligero-Medio | ✅ Sí                |
| `action` (animación)             | Medio        | ✅ Sí                |
| `material`                       | Ligero       | ✅ Sí                |
| `mesh` (vértices, caras)         | Muy pesado   | ❌ No                |
| `image` (texturas)               | Muy pesado   | ❌ No                |
| `volume`                         | Muy pesado   | ❌ No                |

4. **Canal ZMQ directo**: Las transformaciones interactivas deben ir por un canal separado que no pase por el sistema de repositorio (`porcelain`). Esto evita generar commits basura en el historial del repositorio y no bloquea la interfaz del Host.
5. **Un solo keyframe por hueso editado**: En el Cliente, solo se mantiene UN keyframe temporal por hueso en edición. Se actualiza su valor, no se recrea el keyframe en cada update.
6. **Limpieza de memoria**: Los valores originales guardados para restauración deben liberarse inmediatamente después de que la edición se confirma o se cancela. No acumular datos en memoria.

### 9. EDGE CASES Y CASOS LÍMITE A CONSIDERAR

1. **Host cancela la transformación (click derecho / ESC)**: El Cliente debe restaurar los valores originales del keyframe en el frame del Host. Si no existía keyframe previo, el keyframe temporal se elimina completamente. No debe quedar rastro de la edición cancelada.
2. **Host confirma la transformación (click izquierdo / Enter)**: El commit/push real por `porcelain` actualiza la animación. El keyframe temporal del Cliente debe limpiarse y ser reemplazado por los datos reales confirmados del repositorio.
3. **Host cambia de frame mientras edita**: Si el Host se mueve a otro frame durante la edición interactiva, el keyframe temporal en el Cliente debe moverse al nuevo frame. Se debe eliminar el keyframe del frame anterior y crear/actualizar uno en el nuevo frame. Los valores originales del frame anterior deben restaurarse.
4. **Múltiples huesos editados simultáneamente**: Aunque en la práctica Blender solo permite una transformación interactiva a la vez, el sistema debe ser robusto y capaz de manejar múltiples keyframes temporales si fuera necesario.
5. **Cliente sin animación (sin Acción asignada)**: Si el Cliente no tiene una Acción asignada al armazón, el sistema debe manejar esto gracefully. Puede simplemente aplicar la matriz directamente al hueso sin necesidad de keyframes, o crear una Acción para alojar el keyframe.
6. **Host edita un frame donde no existe keyframe previo**: Esto es un comportamiento esperado y normal manejado por los casos anteriores.
7. **Host edita un frame donde SÍ existe keyframe previo**: El Cliente debe guardar el valor original del keyframe existente antes de sobrescribirlo con el valor temporal. Al cancelar, restaura el valor original. Al confirmar, el commit/push real reemplaza el dato.
8. **Desconexión del Cliente durante una edición**: Si el Cliente se desconecta mientras el Host está editando, no debe haber errores en el Host. Al reconectarse, el Cliente debe recibir el estado actual completo del repositorio y no debe tener keyframes temporales huérfanos. En caso extremo, el cliente simplemente se debe crear otra copia del .blend original y reconectarse.
9. **Desconexión del Host durante una edición**: Si el Host se desconecta mientras está editando, el Cliente debe detectar la pérdida de conexión, limpiar cualquier keyframe temporal pendiente, y restaurar los valores originales. En caso extremo, el cliente simplemente se debe crear otra copia del .blend original y reconectarse.
10. **Host edita un hueso que no existe en el Cliente**: Si por alguna razón el hueso editado no existe en la escena del Cliente (error de sincronización), el sistema debe ignorar las señales de update gracefully sin crashear. El usuario tendra que volver a copiar el .blend original y reconectarse.
11. **Toggle de Live Sync NO activo al confirmar**: Si el Host confirma la transformación pero el toggle de Live Sync no está activo para el data block afectado, el Cliente limpia el keyframe temporal pero no recibe los datos confirmados hasta que el Host haga commit manual. Esto puede causar un breve vacío visual. (Ver Sección 6.5.)..

### 10. ESTRUCTURA DE ARCHIVOS DEL ADDON (Referencia)

```
multi_user/
├── __init__.py              # Registro del addon, propiedades globales, register/unregister
├── ui.py                    # Paneles y dibujo de UI
│                            #   - draw_property(): dibuja cada data block con botones de commit/push
│                            #   - SESSION_PT_repository: panel del repositorio
│                            #   - SESSION_PT_sync: panel de sincronización
├── operators.py             # Operadores del addon
│                            #   - SessionCommit (wm.session_datablock_commit)
│                            #   - SessionApply (wm.session_datablock_revert)
│                            #   - SessionPropertyRightOperator (wm.session_datablock_owner_set)
│                            #   - SessionPropertyRemoveOperator (wm.session_datablock_ignore)
├── handlers.py              # Handlers de Blender
│                            #   - on_scene_update: depsgraph_update_post handler
│                            #   - update_client_frame: frame_change_pre handler
│                            #   - sanitize_deps_graph, resolve_deps_graph
├── presence.py              # Presencia de usuarios online
├── libs/
│   └── replication/
│       ├── interface.py     # Interfaz de sesión
│       ├── session.py       # Lógica de sesión y conexión
│       ├── porcelain.py     # Operaciones de repositorio
│       │                    #   - commit(), push(), apply(), lock(), unlock(), rm(), add()
│       ├── rdp.py           # Replication Data Protocol
│       └── live_sync.py     # [NUEVO] Motor de Live Sync para Modo Pose
└── icons/                   # Iconos personalizados del addon
```

### 11. DEPENDENCIAS Y HERRAMIENTAS DEL ADDON ORIGINAL

- **ZeroMQ (ZMQ)**: Librería de mensajería usada para toda la comunicación de red entre Host y Cliente. El addon requiere permisos de red para funcionar.
- **Porcelain**: Capa de abstracción sobre el sistema de replicación. Provee operaciones tipo Git: `commit`, `push`, `apply`, `lock`, `unlock`, `rm`, `add`, `purge_orphan_nodes`.
- **Depsgraph**: Blender Dependency Graph. Usado para detectar cambios en la escena mediante el handler `depsgraph_update_post`.
- **Blender Python API (bpy)**: Acceso a datos de la escena, operadores, handlers, keyframes, F-Curves, depsgraph evaluado, etc.
- **Timers de Blender**: `bpy.app.timers.register()` para tareas periódicas no bloqueantes.
- **Handlers de Blender**: `bpy.app.handlers.depsgraph_update_post`, `frame_change_pre`, `undo_post`, `redo_post`, `load_pre`.

### 12. RESUMEN EJECUTIVO

Se modifica el addon Multi-User de Blender para añadir dos funcionalidades principales que operan de forma complementaria:

**Funcionalidad 1: Toggle Global de Live Sync**  
Convierte los botones individuales de commit/push en toggles que activan/desactivan la sincronización constante por data block. Controlado por un toggle global maestro. Funciona mediante el handler `depsgraph_update_post` y solo detecta cambios confirmados (escritos en `bpy.data`). Es completamente ciega a las transformaciones interactivas en curso (`R`, `G`, `S` antes de confirmar).

**Funcionalidad 2: Live Sync de Modo Pose**  
Permite que las transformaciones interactivas de huesos (`R`, `G`, `S` antes de confirmar) se vean en tiempo real en el Cliente. Usa `evaluated_get(depsgraph)` para leer la pose visual y un canal ZMQ directo para enviarla. En el Cliente, la pose se manifiesta como un keyframe temporal insertado o sobrepuesto en el frame específico donde el Host está editando. La animación del Cliente es modificada temporalmente pero de forma completamente reversible: los valores originales se guardan antes de la sobreescritura y se restauran si el Host cancela. El hueso NO se silencia, NO se congela, y el timeline del Cliente NO se pausa.

**Coordinación entre ambas:**  
Durante la fase interactiva (antes de confirmar), solo la Funcionalidad 2 está activa. En el momento de la confirmación, la Funcionalidad 2 envía la señal de fin y la Funcionalidad 1 toma el relevo para hacer el commit/push de los datos confirmados. En caso de cancelación, solo la Funcionalidad 2 actúa para restaurar el estado original.

**La regla de oro:**  
El Cliente es un visor en bucle que nunca se detiene (solo el usuario puede manipular manualmente el timeline). El Host es un editor libre. La edición del Host se integra visualmente en el bucle del Cliente mediante un keyframe temporal en el frame específico de edición. La animación del Cliente se modifica temporalmente pero siempre de forma reversible.

_Documento generado como referencia completa para la implementación. Incluye todos los detalles, restricciones, decisiones, correcciones y aclaraciones discutidos durante la fase de diseño._
