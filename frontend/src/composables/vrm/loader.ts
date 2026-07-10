import type { VRM } from '@pixiv/three-vrm'

import { VRMLoaderPlugin, VRMUtils } from '@pixiv/three-vrm'
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js'

/**
 * Loads a VRM model, applying the same post-processing as the legacy viewer
 * (`res/synth_webui/js/vrm-viewer.mjs`).
 *
 * IMPORTANT: never call `VRMUtils.combineSkeletons()` or
 * `removeUnnecessaryVertices()` here — they rename/merge bones and break the
 * Mixamo→VRM retargeting, which relies on exact `getNormalizedBoneNode()`
 * names. See pixiv/three-vrm#1351.
 */
export async function loadVrm(
  url: string,
  onProgress?: (fraction: number) => void,
): Promise<VRM> {
  const loader = new GLTFLoader()
  loader.setCrossOrigin('anonymous')
  loader.register(parser => new VRMLoaderPlugin(parser))

  const gltf = await loader.loadAsync(url, (event) => {
    if (onProgress && event.total > 0)
      onProgress(event.loaded / event.total)
  })
  const vrm = gltf.userData.vrm as VRM | undefined
  if (!vrm)
    throw new Error(`Model at ${url} is not a VRM`)

  if (vrm.meta?.metaVersion === '0')
    VRMUtils.rotateVRM0(vrm)

  // Face the default camera and never frustum-cull skinned meshes (bones can
  // move geometry outside the original bounds).
  vrm.scene.rotation.y = Math.PI
  vrm.scene.position.set(0, 0, 0)
  vrm.scene.traverse((obj) => {
    obj.frustumCulled = false
  })

  return vrm
}

export function disposeVrm(vrm: VRM): void {
  VRMUtils.deepDispose(vrm.scene)
}
