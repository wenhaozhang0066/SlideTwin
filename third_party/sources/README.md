# Sources shipped in the Docker image

libquadmath (LGPL-2.1-or-later) is bundled, prebuilt, by the numpy and scipy wheels the image
installs. Their notices point to the GCC repository but give no version, and the libraries carry
no compiler version. Their symbol versions do: libgfortran exports GFORTRAN_8 only, one
libquadmath exports QUADMATH_1.1 (GCC 8, the manylinux_2_28 toolchain) and another QUADMATH_1.0
only (GCC 4.8, the manylinux2014 toolchain). These archives hold the `libquadmath` folder and
`COPYING.LIB` of those GCC releases, taken from the release archives on ftp.gnu.org, whose GNU
signatures were checked against the GNU keyring on 2026-10-16:

| Archive | From | Signed by | sha256 of the release archive |
|---|---|---|---|
| libquadmath-gcc-8.5.0.tar.xz | gcc-8.5.0.tar.xz | Jakub Jelinek | d308841a511bb830a6100397b0042db24ce11f642dab6ea6ee44842e5325ed50 |
| libquadmath-gcc-4.8.5.tar.xz | gcc-4.8.5.tar.bz2 | Richard Guenther | 22fb1e7e0f68a63cee631d85b20461d1ea6bda162f03096350e38c8d427ecf23 |

The versions are inferred from the symbols; distributions may patch GCC. If a published image must
be beyond doubt, add a written offer of the corresponding source as well.
