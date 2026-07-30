import io
import pytest
import torch
from unittest.mock import MagicMock
from PIL import Image
import pixiv_node
from pixiv_node import PixivBrowser


def _make_mock_client(w=64, h=64):
    client = MagicMock()
    img = Image.new("RGB", (w, h), color=(128, 64, 200))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    client.download_image_bytes.return_value = buf.getvalue()
    client.get_original_url.return_value = "https://i.pximg.net/orig/img.jpg"
    return client


def test_execute_returns_image_list(monkeypatch):
    monkeypatch.setattr(pixiv_node, "_get_client", lambda: _make_mock_client())
    result = PixivBrowser().execute(artwork_ids="12345")
    images = result[0]
    assert isinstance(images, list)
    assert len(images) == 1
    tensor = images[0]
    assert isinstance(tensor, torch.Tensor)
    assert tensor.ndim == 4
    assert tensor.shape[0] == 1
    assert tensor.shape[3] == 3
    assert tensor.dtype == torch.float32
    assert tensor.min() >= 0.0 and tensor.max() <= 1.0


def test_execute_multiple_ids_returns_list(monkeypatch):
    monkeypatch.setattr(pixiv_node, "_get_client", lambda: _make_mock_client())
    result = PixivBrowser().execute(artwork_ids="111,222,333")
    assert len(result[0]) == 3


def test_execute_preserves_each_image_original_size(monkeypatch):
    client = MagicMock()
    encoded = []
    for size, color in [((40, 80), "white"), ((80, 40), "red")]:
        buf = io.BytesIO()
        Image.new("RGB", size, color=color).save(buf, format="PNG")
        encoded.append(buf.getvalue())
    client.download_image_bytes.side_effect = encoded
    monkeypatch.setattr(pixiv_node, "_get_client", lambda: client)

    images = PixivBrowser().execute(
        artwork_ids="1|https://i.pximg.net/1.png,2|https://i.pximg.net/2.png"
    )[0]

    assert images[0].shape == (1, 80, 40, 3)
    assert images[1].shape == (1, 40, 80, 3)


def test_execute_skips_failed_downloads(monkeypatch):
    client = MagicMock()
    client.get_original_url.return_value = "https://i.pximg.net/img.jpg"
    img = Image.new("RGB", (32, 32))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    client.download_image_bytes.side_effect = [Exception("timeout"), buf.getvalue()]
    monkeypatch.setattr(pixiv_node, "_get_client", lambda: client)
    result = PixivBrowser().execute(artwork_ids="111,222")
    assert len(result[0]) == 1


def test_execute_downloads_selected_post_pages(monkeypatch):
    client = _make_mock_client()
    monkeypatch.setattr(pixiv_node, "_get_client", lambda: client)

    result = PixivBrowser().execute(
        artwork_ids=(
            "123:p0|https://i.pximg.net/orig/p0.jpg,"
            "123:p2|https://i.pximg.net/orig/p2.jpg"
        )
    )

    assert len(result[0]) == 2
    client.get_original_url.assert_not_called()


def test_execute_resolves_page_url_when_not_serialized(monkeypatch):
    client = _make_mock_client()
    monkeypatch.setattr(pixiv_node, "_get_client", lambda: client)

    PixivBrowser().execute(artwork_ids="123:p1")

    client.get_original_url.assert_called_once_with(123, page_index=1)


def test_execute_raises_on_empty_ids(monkeypatch):
    monkeypatch.setattr(pixiv_node, "_get_client", lambda: MagicMock())
    with pytest.raises(ValueError, match="请先在弹窗中选择图片"):
        PixivBrowser().execute(artwork_ids="")


def test_input_types_has_artwork_ids():
    assert "artwork_ids" in PixivBrowser.INPUT_TYPES()["required"]


def test_return_types_is_image():
    assert PixivBrowser.RETURN_TYPES == ("IMAGE",)
    assert PixivBrowser.OUTPUT_IS_LIST == (True,)
