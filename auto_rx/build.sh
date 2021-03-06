#!/bin/bash
#
# Auto Sonde Decoder build script.
#

# Build rs_detect.
echo "Building dft_detect"
cd ../scan/
gcc dft_detect.c -lm -o dft_detect -DNOC34C50

echo "Building RS92/RS41/DFM Demodulators"
cd ../demod/
gcc -c demod.c
gcc -c demod_dft.c
gcc rs92dm_dft.c demod_dft.o -lm -o rs92ecc -I../ecc/ -I../rs92
gcc rs41dm_dft.c demod_dft.o -lm -o rs41ecc -I../ecc/ -I../rs41 -w
gcc dfm09dm_dft.c demod_dft.o -lm -o dfm09ecc -I../ecc/ -I../dfm

# New demodulators
cd ../demod/mod/
gcc -c demod_mod.c -w
gcc -c bch_ecc_mod.c -w
gcc rs41mod.c demod_mod.o bch_ecc_mod.o -lm -o rs41mod -w
# Holding off on DFM decoder until the DFM17/15 ID issue is resolved.
#gcc dfm09mod.c demod_mod.o -lm -o dfm09mod -w
gcc rs92mod.c demod_mod.o bch_ecc_mod.o -lm -o rs92mod -w
#gcc lms6mod.c demod_mod.o bch_ecc_mod.o -lm -o lms6mod -w
#gcc m10mod.c demod_mod.o -lm -o m10mod -w


# Build M10 decoder
echo "Building M10 Demodulator."
cd ../../m10/
g++ M10.cpp M10Decoder.cpp M10GeneralParser.cpp M10GtopParser.cpp M10TrimbleParser.cpp AudioFile.cpp -lm -o m10 -std=c++11

echo "Building iMet Demodulator."
cd ../imet/
gcc imet1rs_dft.c -lm -o imet1rs_dft

# Copy all necessary files into this directory.
echo "Copying files into auto_rx directory."
cd ../auto_rx/
cp ../scan/dft_detect .
cp ../demod/rs92ecc .
cp ../demod/rs41ecc .
cp ../demod/dfm09ecc .
cp ../m10/m10 .
cp ../imet/imet1rs_dft .

cp ../demod/mod/rs41mod .
#cp ../demod/mod/dfm09mod .
cp ../demod/mod/rs92mod .
#cp ../demod/mod/lms6mod .

echo "Done!"
