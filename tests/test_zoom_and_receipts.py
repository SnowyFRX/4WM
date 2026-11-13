import unittest
from types import SimpleNamespace
from unittest.mock import patch

from jwm import mapview
from jwm.route_camera import RouteCamera
from jwm.route_map import RouteMap
from jwm.tasks.recruit import Recruit


class MapZoomTests(unittest.TestCase):
    def tearDown(self):
        mapview.set_tile_size(67.5)

    def test_zoom_updates_coordinate_conversion(self):
        camera = RouteCamera(SimpleNamespace())
        camera.anchor = (360, 640, 243, 291)
        mapview.set_tile_size(90)
        self.assertEqual(camera.xy((450, 550)), (244, 292))
        self.assertEqual(camera.xy((270, 730)), (242, 290))

    def test_zoom_rebuilds_scaled_sprite_bank(self):
        mapview._bank = object()
        mapview.set_tile_size(54)
        self.assertIsNone(mapview._bank)

    def test_implausible_selection_size_is_rejected(self):
        with self.assertRaises(ValueError):
            mapview.set_tile_size(20)

    def test_normal_tile_borders_correct_each_frame_phase(self):
        class Image:
            def __getitem__(self, item):
                return self

        camera = RouteCamera(SimpleNamespace())
        camera.anchor = (360, 640, 243, 291)
        mapview.set_tile_size(70)
        with patch("jwm.route_camera.mv.estimate_phase", return_value=(50, 40)), \
                patch("jwm.route_camera.mv.MapGrid", return_value="grid") as grid:
            self.assertEqual(camera._capture_from(Image(), None), "grid")
        self.assertEqual(camera.anchor, (365, 625, 243, 291))
        self.assertEqual(grid.call_args.kwargs["phase"], (50, 30))

    def test_frame_publishes_tile_and_drops_other_zoom_images(self):
        route = RouteMap()
        tiles = [dict(x=1, y=1, left=0, top=270, kind="land", res=None, level=1)]
        with patch.object(route, "_cache_load", return_value=0), \
                patch.object(route, "_route_load", return_value=False), \
                patch.object(route, "_cache_save"):
            route.publish((1, 1), (1, 1), tiles, b"first", tile=90)
            self.assertEqual(route.view["tile"], 90)
            route.publish((1, 1), (1, 1), tiles, b"second", tile=54)
        self.assertEqual(route.view["tile"], 54)
        self.assertEqual(len(route.frames), 1)
        self.assertEqual(next(iter(route.frames.values()))[1], b"second")


class RecruitReceiptTests(unittest.TestCase):
    def make_recruit(self, after):
        bot = SimpleNamespace(dev=SimpleNamespace(tap=lambda *a, **k: None),
                              ui=SimpleNamespace(grab=lambda: after,
                                                 grab_stable=lambda: after))
        recruit = Recruit.__new__(Recruit)
        recruit.dev = bot.dev
        recruit.ui = bot.ui
        recruit.resource_shortage = False
        recruit.queue_full = False
        return recruit

    def test_resource_rejection_is_not_counted(self):
        after = SimpleNamespace(img=None)
        after.text = lambda pattern: "资源不足"
        recruit = self.make_recruit(after)
        with patch.object(recruit, "slot_selected", return_value=True), \
                patch.object(recruit, "queue", side_effect=[(0, 3), (0, 3)]), \
                patch.object(recruit, "capacity", side_effect=[(1, 9), (1, 9)]), \
                patch.object(recruit, "button_active", return_value=True):
            self.assertEqual(recruit.fill("编队1", 1), 0)
        self.assertTrue(recruit.resource_shortage)

    def test_unchanged_queue_is_not_counted(self):
        after = SimpleNamespace(img=None)
        after.text = lambda pattern: None
        recruit = self.make_recruit(after)
        with patch.object(recruit, "slot_selected", return_value=True), \
                patch.object(recruit, "queue", side_effect=[(0, 3), (0, 3)]), \
                patch.object(recruit, "capacity", side_effect=[(1, 9), (1, 9)]), \
                patch.object(recruit, "button_active", return_value=True):
            self.assertEqual(recruit.fill("编队1", 1), 0)

    def test_queue_increase_is_counted_once(self):
        after = SimpleNamespace(img=None)
        after.text = lambda pattern: None
        recruit = self.make_recruit(after)
        with patch.object(recruit, "slot_selected", return_value=True), \
                patch.object(recruit, "queue", side_effect=[(0, 3), (1, 3), (1, 3)]), \
                patch.object(recruit, "capacity", side_effect=[(1, 9), (2, 9), (2, 9), (2, 9)]), \
                patch.object(recruit, "button_active", side_effect=[True, False]):
            self.assertEqual(recruit.fill("编队1", 1), 1)


if __name__ == "__main__":
    unittest.main()
