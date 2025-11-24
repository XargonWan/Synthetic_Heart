import * as THREE from 'three';
import { FBXLoader } from 'three/addons/loaders/FBXLoader.js';
import { mixamoVRMRigMap } from './mixamoVRMRigMap.js';

/**
 * Load Mixamo animation, convert for three-vrm use, and return it.
 *
 * @param {string} url A url of mixamo animation data
 * @param {VRM} vrm A target VRM
 * @returns {Promise<THREE.AnimationClip>} The converted AnimationClip
 */
export function loadMixamoAnimation( url, vrm ) {
	const msg = `[loadMixamoAnimation] ⭐ FUNCTION CALLED with url: ${url}, vrm: ${!!vrm}`;
	console.log(msg);
	if (window.SynthWebUISetStatus) window.SynthWebUISetStatus(msg);
	
	const loader = new FBXLoader(); // A loader which loads FBX
	return loader.loadAsync( url ).then( ( asset ) => {
		try {
			console.log(`[loadMixamoAnimation] ✓ FBX loaded from ${url}, asset.animations.length: ${asset.animations?.length || 0}`);
			let clip = THREE.AnimationClip.findByName( asset.animations, 'mixamo.com' ); // extract the AnimationClip
			
			if (!clip && asset.animations && asset.animations.length > 0) {
				console.warn(`[loadMixamoAnimation] ⚠ 'mixamo.com' not found. Trying first available animation...`);
				console.warn(`[loadMixamoAnimation] Available animations: ${asset.animations.map(a => a.name).join(', ')}`);
				// Fallback: use first animation
				clip = asset.animations[0];
				console.log(`[loadMixamoAnimation] Using fallback animation: ${clip.name}`);
			}
			
			console.log(`[loadMixamoAnimation] Loaded FBX from ${url}, clip name: ${clip?.name}, duration: ${clip?.duration}`);
			console.log(`[loadMixamoAnimation] Clip has ${clip?.tracks?.length || 0} original tracks`);
			console.log(`[loadMixamoAnimation] VRM humanoid available: ${vrm.humanoid ? 'YES' : 'NO'}`);
			if (vrm.humanoid) {
				const hips = vrm.humanoid.getNormalizedBoneNode('hips');
				console.log(`[loadMixamoAnimation] VRM hips bone: ${hips ? 'FOUND' : 'MISSING'}`);
			}
			
			if (!clip) {
				const msg = `[loadMixamoAnimation] ❌ No animation clip found, available: ${asset.animations?.map(a => a.name).join(', ') || 'NONE'}`;
				console.error(msg);
				if (window.SynthWebUISetStatus) window.SynthWebUISetStatus(msg, 'error');
				throw new Error('No animation clip found in FBX file');
			}

			const tracks = []; // KeyframeTracks compatible with VRM will be added here

			const restRotationInverse = new THREE.Quaternion();
			const parentRestWorldRotation = new THREE.Quaternion();
			const _quatA = new THREE.Quaternion();
			const _vec3 = new THREE.Vector3();

			// Adjust with reference to hips height.
			const motionHipsHeight = asset.getObjectByName( 'mixamorigHips' ).position.y;
			const vrmHipsY = vrm.humanoid?.getNormalizedBoneNode( 'hips' ).getWorldPosition( _vec3 ).y;
			const vrmRootY = vrm.scene.getWorldPosition( _vec3 ).y;
			const vrmHipsHeight = Math.abs( vrmHipsY - vrmRootY );
			const hipsPositionScale = vrmHipsHeight / motionHipsHeight;
			
			let processedBones = 0;
			let skippedBones = 0;

			clip.tracks.forEach( ( track ) => {

				// Convert each tracks for VRM use, and push to `tracks`
				const trackSplitted = track.name.split( '.' );
				const mixamoRigName = trackSplitted[ 0 ];
				const vrmBoneName = mixamoVRMRigMap[ mixamoRigName ];
				const vrmNodeName = vrm.humanoid?.getNormalizedBoneNode( vrmBoneName )?.name;
				const mixamoRigNode = asset.getObjectByName( mixamoRigName );

				if ( vrmNodeName != null ) {
					processedBones++;

					const propertyName = trackSplitted[ 1 ];

					// Store rotations of rest-pose.
					mixamoRigNode.getWorldQuaternion( restRotationInverse ).invert();
					mixamoRigNode.parent.getWorldQuaternion( parentRestWorldRotation );

					if ( track instanceof THREE.QuaternionKeyframeTrack ) {

						// Retarget rotation of mixamoRig to NormalizedBone.
						for ( let i = 0; i < track.values.length; i += 4 ) {

							const flatQuaternion = track.values.slice( i, i + 4 );

							_quatA.fromArray( flatQuaternion );

							// 親のレスト時ワールド回転 * トラックの回転 * レスト時ワールド回転の逆
							_quatA
								.premultiply( parentRestWorldRotation )
								.multiply( restRotationInverse );

							_quatA.toArray( flatQuaternion );

							flatQuaternion.forEach( ( v, index ) => {

								track.values[ index + i ] = v;

							} );

						}

						tracks.push(
							new THREE.QuaternionKeyframeTrack(
								`${vrmNodeName}.${propertyName}`,
								track.times,
								track.values.map( ( v, i ) => ( vrm.meta?.metaVersion === '0' && i % 2 === 0 ? - v : v ) ),
							),
						);

					} else if ( track instanceof THREE.VectorKeyframeTrack ) {

						const value = track.values.map( ( v, i ) => ( vrm.meta?.metaVersion === '0' && i % 3 !== 1 ? - v : v ) * hipsPositionScale );
						tracks.push( new THREE.VectorKeyframeTrack( `${vrmNodeName}.${propertyName}`, track.times, value ) );

					}

				} else {
					skippedBones++;
					console.warn(`[loadMixamoAnimation] WARNING: Could not find VRM bone for Mixamo rig "${mixamoRigName}" (bone name: "${vrmBoneName}")`);
				}

			} );

			console.log(`[loadMixamoAnimation] Converted ${tracks.length} tracks for VRM animation (processed: ${processedBones}, skipped: ${skippedBones})`);
			const vrmAnimClip = new THREE.AnimationClip( 'vrmAnimation', clip.duration, tracks );
			const msg = `[loadMixamoAnimation] ✓ Created VRM clip: ${vrmAnimClip.tracks.length} tracks, ${vrmAnimClip.duration.toFixed(2)}s`;
			console.log(msg);
			if (window.SynthWebUISetStatus) window.SynthWebUISetStatus(msg);
			return vrmAnimClip;
		} catch ( err ) {
			console.error(`[loadMixamoAnimation] ERROR converting animation: ${err.message}`, err);
			throw err;
		}

	} ).catch(err => {
		const msg = `[loadMixamoAnimation] ❌ ERROR loading FBX from ${url}: ${err.message}`;
		console.error(msg);
		if (window.SynthWebUISetStatus) window.SynthWebUISetStatus(msg, 'error');
		throw err;
	});

}
