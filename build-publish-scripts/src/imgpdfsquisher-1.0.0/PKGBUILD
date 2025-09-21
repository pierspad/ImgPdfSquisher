# Maintainer: Your Name <you at example dot com>
pkgname=imgpdfsquisher
pkgver=1.0.0
pkgrel=1
pkgdesc="A tool to compress images in PDF files"
arch=('any')
url="https://github.com/pierspad/imgpdfsquisher"
license=('MIT')
depends=('ghostscript' 'imagemagick' 'poppler' 'python')
makedepends=('git' 'python-build' 'python-installer' 'python-wheel')
source=("git+https://github.com/pierspad/imgpdfsquisher.git")
sha256sums=('SKIP')

pkgver() {
  cd "$srcdir/imgpdfsquisher"
  git describe --tags --long 2>/dev/null | sed 's/^v//; s/-/./g'
}

build() {
  cd "$srcdir/imgpdfsquisher"
  python -m build --wheel --no-isolation
}

package() {
  cd "$srcdir/imgpdfsquisher"
  python -m installer --destdir="$pkgdir" dist/*.whl
  install -Dm644 LICENSE "$pkgdir/usr/share/licenses/$pkgname/LICENSE" || true
  install -Dm644 README* "$pkgdir/usr/share/doc/$pkgname/README" || true
}
