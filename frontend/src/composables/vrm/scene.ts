import * as THREE from 'three'
import { OrbitControls } from 'three/addons/controls/OrbitControls.js'

export interface CameraPreset {
  position: [number, number, number]
  target: [number, number, number]
}

/** Framing presets. Values derived from the legacy viewer's defaults. */
export const CAMERA_PRESETS: Record<'portrait' | 'full-body', CameraPreset> = {
  'portrait': { position: [0, 1.38, 1.15], target: [0, 1.3, 0] },
  'full-body': { position: [0, 1.4, 2.2], target: [0, 1.0, 0] },
}

export interface SceneHost {
  renderer: THREE.WebGLRenderer
  scene: THREE.Scene
  camera: THREE.PerspectiveCamera
  /** Group holding the camera. Desktop mode moves this rig; a future WebXR
   * session leaves the camera to XR and repositions `avatarRoot` instead. */
  cameraRig: THREE.Group
  /** Group the VRM is parented under — the AR hit-test anchor point. */
  avatarRoot: THREE.Group
  controls: OrbitControls
  /** Register a per-frame callback (receives delta seconds). */
  onFrame: (cb: (delta: number) => void) => void
  applyCameraPreset: (preset: CameraPreset) => void
  setSize: (width: number, height: number) => void
  dispose: () => void
}

/**
 * Owns the renderer, scene graph skeleton, and the render loop.
 *
 * AR-readiness rules enforced here (do not "fix" them):
 * - the loop runs through `renderer.setAnimationLoop`, never rAF, so a WebXR
 *   session can take over frame scheduling;
 * - nothing outside this module touches `camera` directly — consumers get
 *   `cameraRig` and `avatarRoot`;
 * - the canvas is alpha so transparent/OBS mode and AR passthrough are free.
 */
export function createSceneHost(canvas: HTMLCanvasElement): SceneHost {
  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true })
  renderer.outputColorSpace = THREE.SRGBColorSpace
  renderer.setPixelRatio(window.devicePixelRatio)

  const scene = new THREE.Scene()

  const camera = new THREE.PerspectiveCamera(
    30,
    canvas.clientWidth / Math.max(1, canvas.clientHeight),
    0.1,
    20,
  )
  const cameraRig = new THREE.Group()
  cameraRig.add(camera)
  scene.add(cameraRig)

  const avatarRoot = new THREE.Group()
  scene.add(avatarRoot)

  // Studio lighting rig (matches the legacy viewer's levels). Swappable as a
  // unit when AR estimated lighting arrives.
  const lights = new THREE.Group()
  lights.add(new THREE.AmbientLight(0xFFFFFF, 0.6))
  const keyLight = new THREE.DirectionalLight(0xFFFFFF, 1.2)
  keyLight.position.set(1, 1.2, 1)
  lights.add(keyLight)
  const fillLight = new THREE.DirectionalLight(0xFFFFFF, 0.4)
  fillLight.position.set(-1, 1.2, -1)
  lights.add(fillLight)
  scene.add(lights)

  const controls = new OrbitControls(camera, canvas)
  controls.enableDamping = true
  controls.dampingFactor = 0.05

  const frameCallbacks: Array<(delta: number) => void> = []
  const timer = new THREE.Timer()

  renderer.setAnimationLoop((time: number) => {
    timer.update(time)
    const delta = timer.getDelta()
    for (const cb of frameCallbacks)
      cb(delta)
    controls.update()
    renderer.render(scene, camera)
  })

  function applyCameraPreset(preset: CameraPreset): void {
    camera.position.set(...preset.position)
    controls.target.set(...preset.target)
    controls.update()
  }

  function setSize(width: number, height: number): void {
    renderer.setSize(width, height, false)
    camera.aspect = width / Math.max(1, height)
    camera.updateProjectionMatrix()
  }

  function onFrame(cb: (delta: number) => void): void {
    frameCallbacks.push(cb)
  }

  function dispose(): void {
    renderer.setAnimationLoop(null)
    controls.dispose()
    renderer.dispose()
  }

  applyCameraPreset(CAMERA_PRESETS['full-body'])

  return {
    renderer,
    scene,
    camera,
    cameraRig,
    avatarRoot,
    controls,
    onFrame,
    applyCameraPreset,
    setSize,
    dispose,
  }
}
