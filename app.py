"""Vercel Python entrypoint for the ImgClean API."""
from imgclean_web.app import create_app


app = create_app()
