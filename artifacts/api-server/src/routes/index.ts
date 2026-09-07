import { Router, type IRouter } from "express";
import healthRouter from "./health";
import propLineRouter from "./propline";

const router: IRouter = Router();

router.use(healthRouter);
router.use(propLineRouter);

export default router;
