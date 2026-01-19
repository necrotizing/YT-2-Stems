#!/usr/bin/env python3
"""
Demucs runner wrapper that patches torchaudio to use soundfile backend.
This bypasses the torchcodec requirement in newer torchaudio versions.

Usage: python demucs_runner.py [demucs arguments...]
"""

import sys

def patch_torchaudio():
    """Patch torchaudio.save to use soundfile instead of torchcodec."""
    import torch
    import soundfile as sf
    import numpy as np
    
    # Store original save function
    import torchaudio
    _original_save = torchaudio.save
    
    def patched_save(uri, src, sample_rate, channels_first=True, format=None, 
                     encoding=None, bits_per_sample=None, compression=None, **kwargs):
        """
        Replacement for torchaudio.save that uses soundfile.
        """
        # Convert tensor to numpy
        if channels_first:
            # (channels, samples) -> (samples, channels)
            audio_np = src.cpu().numpy().T
        else:
            audio_np = src.cpu().numpy()
        
        # Determine format from uri if not specified
        if format is None:
            format = str(uri).rsplit('.', 1)[-1].lower()
        
        # Map format to soundfile subtype
        subtype = None
        if format == 'wav':
            if bits_per_sample == 16:
                subtype = 'PCM_16'
            elif bits_per_sample == 24:
                subtype = 'PCM_24'
            elif bits_per_sample == 32:
                subtype = 'PCM_32'
            else:
                subtype = 'FLOAT'  # Default to float for WAV
        elif format == 'flac':
            if bits_per_sample == 16:
                subtype = 'PCM_16'
            elif bits_per_sample == 24:
                subtype = 'PCM_24'
            else:
                subtype = 'PCM_24'  # Default for FLAC
        
        # Write using soundfile
        sf.write(str(uri), audio_np, sample_rate, subtype=subtype)
    
    # Apply the patch
    torchaudio.save = patched_save
    print("✓ Patched torchaudio.save to use soundfile backend", file=sys.stderr)


def main():
    # Patch torchaudio BEFORE importing demucs
    patch_torchaudio()
    
    # Now import and run demucs
    from demucs.separate import main as demucs_main
    
    # Remove our script name from argv so demucs sees correct args
    sys.argv = sys.argv[0:1] + sys.argv[1:]
    
    demucs_main()


if __name__ == "__main__":
    main()
